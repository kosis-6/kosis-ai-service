import type { AskResponse, ChartView, ConversationDetail, ConversationList, ConversationTurn } from "../api/types";
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

/** 화면에서 바꾼 차트 모양(꺾은선 · 막대 · 표)을 그 답에 남긴다. 보고서는 서버의 답을 복사해
 *  만들어서, 남기지 않으면 말로 만든 보고서('보고서 만들어줘' · 제안 칩 · '정리해줘')와 다시 연
 *  대화가 처음 모양으로 돌아갔다 */
export async function saveChartView(sessionId: string, turnId: number, view: ChartView): Promise<void> {
  const res = await authFetch(`/conversations/${encodeURIComponent(sessionId)}/turns/${turnId}/chart-view`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ view }),
  });
  if (!res.ok) throw new HistoryError(res.status);
}

export function createChartViewQueue(persist = saveChartView) {
  const pending = new Map<string, Map<number, Promise<void>>>();

  return {
    save(sessionId: string, turnId: number, view: ChartView): Promise<void> {
      const turns = pending.get(sessionId) ?? new Map<number, Promise<void>>();
      pending.set(sessionId, turns);
      const previous = turns.get(turnId);
      const write = () => persist(sessionId, turnId, view);
      const request = previous ? previous.then(write) : write();
      const settled = request.catch(() => {});
      turns.set(turnId, settled);
      void settled.then(() => {
        if (turns.get(turnId) === settled) turns.delete(turnId);
        if (turns.size === 0) pending.delete(sessionId);
      });
      return request;
    },
    async wait(sessionId: string): Promise<void> {
      await Promise.all(pending.get(sessionId)?.values() ?? []);
    },
  };
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
