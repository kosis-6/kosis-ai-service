// 로그인 상태와 서버 로그인 API(/auth/*) 호출
//
// 토큰 보관
//   Access Token   이 파일의 변수(메모리)에만 둔다. localStorage 에 두면 XSS 로 읽어 갈 수 있다
//   Refresh Token  서버가 HttpOnly 쿠키로 준다. 화면 코드는 읽을 수 없고 /auth 요청에 브라우저가 붙인다
// 화면을 새로 열면 메모리가 비므로 POST /auth/refresh 로 Access Token 을 다시 받는다.
// 브라우저에는 표시 이름만 남긴다: 로그인한 적이 있는지 알아 refresh 를 부를지 정하고,
// 머리글을 바로 그리려는 것이다. 진짜 로그인 확인은 요청마다 서버가 한다.
import type { Token, UserRead } from "../api/types";

export type User = { name: string };

export type Failure = {
  ok: false;
  status: number; // 0 이면 서버에 닿지 못함
  message: string;
  fields: Record<string, string>; // 입력값 검사(422): 칸 이름 → 문구
  wait: number; // 429 일 때 다시 시도할 수 있을 때까지(초)
};
export type Result<T> = { ok: true; data: T } | Failure;

const KEY = "six.user";
const OFFLINE = "서버에 연결하지 못했어요. 잠시 뒤에 다시 시도해 주세요.";

function readCache(): User | null {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? "null");
    return value && typeof value.name === "string" ? { name: value.name } : null;
  } catch {
    return null;
  }
}

let token: string | null = null;
let user: User | null = readCache();
let refreshing: Promise<boolean> | null = null; // 요청 여러 개가 함께 401 을 받아도 한 번만 보낸다
const listeners = new Set<() => void>();

function remember(next: User | null): void {
  user = next;
  try {
    if (next) localStorage.setItem(KEY, JSON.stringify(next));
    else localStorage.removeItem(KEY);
  } catch {
    /* 저장 못 해도 화면은 동작 */
  }
  listeners.forEach((fn) => fn());
}

function forget(): void {
  token = null;
  remember(null);
}

/** 서버 오류 응답 → Failure. FastAPI 는 detail 을 글자(직접 낸 오류) 또는 목록(입력값 검사 422)으로 준다 */
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
    /* 본문 없음 */
  }
  return toFailure(res.status, body, res.headers.get("Retry-After"));
}

/** 로그인 없이 부르는 POST (가입·로그인). form 이면 OAuth2 폼으로 보낸다 */
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
  refreshing ??= (async () => {
    try {
      const res = await fetch("/auth/refresh", { method: "POST" });
      if (res.ok) {
        token = ((await res.json()) as Token).access_token;
        return true;
      }
      if (res.status === 401) forget(); // 만료·로그아웃·끊김. 서버 오류(5xx)면 그대로 둔다
      return false;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

function withToken(options: RequestInit): RequestInit {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return { ...options, headers };
}

// 만료(401)면 한 번만 새로 받아 다시 부르고, 새로 받지 못하면 로그인 없이(손님으로) 다시 부른다
async function call(url: string, options: RequestInit = {}): Promise<Response> {
  let res = await fetch(url, withToken(options));
  if (res.status === 401 && token) {
    token = null;
    await refresh();
    res = await fetch(url, withToken(options));
  }
  return res;
}

async function loadMe(): Promise<boolean> {
  const res = await call("/auth/me");
  if (!res.ok) return false;
  const me = (await res.json()) as UserRead;
  remember({ name: me.nickname || me.email.split("@")[0] });
  return true;
}

/** 화면을 열 때: 로그인한 적이 있으면 Access Token 을 다시 받는다 */
export const ready: Promise<void> = (async () => {
  if (!user) return;
  if (await refresh()) await loadMe();
})();

/** 로그인했으면 Access Token 을 붙여 부른다 (화면을 열 때의 로그인 확인이 끝난 뒤에) */
export async function authFetch(url: string, options?: RequestInit): Promise<Response> {
  await ready;
  return call(url, options);
}

export async function login(email: string, password: string, keep: boolean): Promise<Result<null>> {
  const r = await post<Token>("/auth/token", { username: email, password, keep: String(keep) }, true);
  if (!r.ok) return r;
  token = r.data.access_token;
  if (!(await loadMe())) remember({ name: email.split("@")[0] });
  return { ok: true, data: null };
}

export async function logout(): Promise<void> {
  try {
    // Access Token 이 없어도 쿠키만으로 서버의 로그인을 끊는다
    await fetch("/auth/logout", withToken({ method: "POST" }));
  } catch {
    /* 서버에 닿지 못해도 화면은 로그아웃 */
  }
  forget();
}

// React 의 useSyncExternalStore 용
export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
export function currentUser(): User | null {
  return user;
}
