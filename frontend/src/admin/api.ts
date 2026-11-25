import type {
  LogSummary,
  PersonList,
  SearchLogDetail,
  SearchLogList,
  SessionDetail,
  SessionList,
} from "../api/types";
import { authFetch } from "../auth/client";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

async function get<T>(path: string, query: Query = {}): Promise<T> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  }
  const url = params.size ? `${path}?${params}` : path;
  let res: Response;
  try {
    res = await authFetch(url);
  } catch {
    throw new ApiError(0, "서버에 연결하지 못했어요. 잠시 뒤에 다시 시도해 주세요.");
  }
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = (await res.json()).detail;
    } catch {
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : `서버 오류 ${res.status}`);
  }
  return (await res.json()) as T;
}

export function sinceDays(days: number, now = new Date()): string {
  return new Date(now.getTime() - days * 86_400_000).toISOString();
}

export type Who = "all" | "user" | "guest";
const guestOf = (who: Who): boolean | undefined => (who === "all" ? undefined : who === "guest");

export const PAGE = 50;

export function summary(days: number): Promise<LogSummary> {
  return get("/admin/logs/summary", { days });
}

export function people(filter: { since: string; who: Who; q: string }, offset: number): Promise<PersonList> {
  return get("/admin/logs/people", {
    since: filter.since,
    guest: guestOf(filter.who),
    q: filter.q,
    limit: PAGE,
    offset,
  });
}

export function sessions(
  filter: { since: string; q: string; owner: number | null },
  offset: number,
): Promise<SessionList> {
  return get("/admin/logs/sessions", {
    since: filter.since,
    user_id: filter.owner ?? undefined,
    guest: filter.owner === null ? true : undefined,
    q: filter.q,
    limit: PAGE,
    offset,
  });
}

export function session(id: string): Promise<SessionDetail> {
  return get(`/admin/logs/sessions/${encodeURIComponent(id)}`);
}

export function searches(
  filter: { since: string; who: Who; q: string; status: string; errors: boolean },
  offset: number,
): Promise<SearchLogList> {
  return get("/admin/logs/searches", {
    since: filter.since,
    guest: guestOf(filter.who),
    q: filter.q,
    status: filter.status,
    has_error: filter.errors ? true : undefined,
    limit: PAGE,
    offset,
  });
}

export function search(id: number): Promise<SearchLogDetail> {
  return get(`/admin/logs/searches/${id}`);
}
