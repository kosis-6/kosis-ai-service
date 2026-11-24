import type { AskResponse, ConversationDetail, ConversationList, ConversationTurn } from "../api/types";
import { authFetch } from "../auth/client";

export class HistoryError extends Error {
  constructor(readonly status: number) {
    super(status === 404 ? "대화를 찾지 못했어요. 지워졌을 수 있어요." : `대화 기록을 불러오지 못했어요. (서버 ${status})`);
  }
}

async function getJson<T>(url: string): Promise<T> {
  const res = await authFetch(url);
  if (!res.ok) throw new HistoryError(res.status);
  return (await res.json()) as T;
}

export function listConversations(limit = 50): Promise<ConversationList> {
  return getJson(`/conversations?limit=${limit}`);
}

export function getConversation(sessionId: string): Promise<ConversationDetail> {
  return getJson(`/conversations/${encodeURIComponent(sessionId)}`);
}

export async function deleteConversation(sessionId: string): Promise<void> {
  const res = await authFetch(`/conversations/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  if (!res.ok && res.status !== 404) throw new HistoryError(res.status);
}

export function replayed(turn: ConversationTurn, sessionId: string): AskResponse {
  const saved = turn.response as Partial<AskResponse>;
  return {
    ...saved,
    status: saved.status ?? null,
    answer: typeof saved.answer === "string" ? saved.answer : "",
    parsed: {},
    result: {},
    sources: Array.isArray(saved.sources) ? saved.sources : [],
    session_id: sessionId,
    question: turn.question,
  };
}

const pad = (n: number) => String(n).padStart(2, "0");

export function when(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day(now) - day(at)) / 86_400_000);
  if (days <= 0) return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
  if (days === 1) return "어제";
  if (at.getFullYear() === now.getFullYear()) return `${at.getMonth() + 1}월 ${at.getDate()}일`;
  return `${at.getFullYear()}. ${at.getMonth() + 1}. ${at.getDate()}.`;
}
