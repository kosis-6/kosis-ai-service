import type { Token, UserRead } from "../api/types";

export type User = { name: string; admin: boolean };

export type Failure = {
  ok: false;
  status: number;
  message: string;
  fields: Record<string, string>;
  wait: number;
};
export type Result<T> = { ok: true; data: T } | Failure;

const KEY = "six.user";
const OFFLINE = "서버에 연결하지 못했어요. 잠시 뒤에 다시 시도해 주세요.";

function readCache(): User | null {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? "null");
    return value && typeof value.name === "string" ? { name: value.name, admin: value.admin === true } : null;
  } catch {
    return null;
  }
}

let token: string | null = null;
let user: User | null = readCache();
let refreshing: Promise<boolean> | null = null;
let sessionVersion = 0;
const listeners = new Set<() => void>();

function remember(next: User | null): void {
  user = next;
  try {
    if (next) localStorage.setItem(KEY, JSON.stringify(next));
    else localStorage.removeItem(KEY);
  } catch {
  }
  listeners.forEach((fn) => fn());
}

function replaceSession(nextToken: string | null): void {
  sessionVersion += 1;
  refreshing = null;
  token = nextToken;
}

function forget(): void {
  replaceSession(null);
  remember(null);
}

export function toFailure(status: number, body: unknown, retryAfter: string | null = null): Failure {
  const detail = (body as { detail?: unknown } | null)?.detail;
  const fields: Record<string, string> = {};
  let message = "";
  if (Array.isArray(detail)) {
    for (const d of detail as { loc: (string | number)[]; msg: string }[]) {
      fields[String(d.loc[d.loc.length - 1])] = d.msg;
    }
  } else if (typeof detail === "string") {
    message = detail;
  }
  return {
    ok: false,
    status,
    message: message || (status >= 500 ? OFFLINE : "요청을 처리하지 못했어요."),
    fields,
    wait: Number(retryAfter) || 0,
  };
}

async function failure(res: Response): Promise<Failure> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
  }
  return toFailure(res.status, body, res.headers.get("Retry-After"));
}

export async function post<T>(url: string, body: Record<string, string>, form = false): Promise<Result<T>> {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: form ? {} : { "Content-Type": "application/json" },
      body: form ? new URLSearchParams(body) : JSON.stringify(body),
    });
    if (!res.ok) return failure(res);
    return { ok: true, data: (await res.json()) as T };
  } catch {
    return { ok: false, status: 0, message: OFFLINE, fields: {}, wait: 0 };
  }
}

function refresh(): Promise<boolean> {
  if (refreshing) return refreshing;
  const version = sessionVersion;
  const pending = (async () => {
    try {
      const res = await fetch("/auth/refresh", { method: "POST" });
      if (res.ok) {
        const { access_token: nextToken } = (await res.json()) as Token;
        if (version !== sessionVersion || !nextToken) return false;
        token = nextToken;
        return true;
      }
      if (res.status === 401 && version === sessionVersion) forget();
      return false;
    } catch {
      return false;
    }
  })().finally(() => {
    if (refreshing === pending) refreshing = null;
  });
  refreshing = pending;
  return pending;
}

function withToken(options: RequestInit): RequestInit {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return { ...options, headers };
}

function currentSession(): number | null {
  return token || user ? sessionVersion : null;
}

function sessionUnavailable(version: number): Response {
  const active = version === sessionVersion;
  const detail = active ? "로그인 확인에 실패했어요. 잠시 뒤에 다시 시도해 주세요." : "로그인이 필요합니다.";
  return new Response(JSON.stringify({ detail }), {
    status: active ? 503 : 401,
    headers: { "Content-Type": "application/json" },
  });
}

async function call(url: string, options: RequestInit = {}, version = currentSession()): Promise<Response> {
  if (version !== null) {
    if (version === sessionVersion && !token) await refresh();
    if (version !== sessionVersion || !token) return sessionUnavailable(version);
  }

  const sent = token;
  const res = await fetch(url, withToken(options));
  if (res.status !== 401 || !sent || version !== sessionVersion) return res;
  if (token === sent) token = null;
  if (!token) await refresh();
  if (version !== sessionVersion || !token) return res;
  return fetch(url, withToken(options));
}

async function loadMe(): Promise<boolean> {
  const version = sessionVersion;
  const res = await call("/auth/me");
  if (!res.ok) return false;
  const me = (await res.json()) as UserRead;
  if (version !== sessionVersion) return false;
  remember({ name: me.nickname || me.email.split("@")[0], admin: me.is_admin });
  return true;
}

export const ready: Promise<void> = (async () => {
  if (!user) return;
  if (await refresh()) await loadMe();
})();

export async function authFetch(url: string, options?: RequestInit): Promise<Response> {
  const version = currentSession();
  await ready;
  return call(url, options, version ?? currentSession());
}

export async function login(email: string, password: string, keep: boolean): Promise<Result<null>> {
  const r = await post<Token>("/auth/token", { username: email, password, keep: String(keep) }, true);
  if (!r.ok) return r;
  replaceSession(r.data.access_token);
  const version = sessionVersion;
  if (!(await loadMe()) && version === sessionVersion) remember({ name: email.split("@")[0], admin: false });
  return { ok: true, data: null };
}

export async function logout(): Promise<void> {
  const options = withToken({ method: "POST" });
  forget();
  try {
    await fetch("/auth/logout", options);
  } catch {
  }
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
export function currentUser(): User | null {
  return user;
}
