// 로그인 유저의 지난 대화 (/conversations, 서버 app/services/conversations.py)
// 서버는 로그인 유저가 /ask 로 나눈 대화의 질문과 그때 보여 준 답을 남긴다.
// 화면은 대화 주소(/chat/c/{session_id})로 그 기록을 다시 그리고, 이어 물으면 같은 session_id 를 보낸다.
// 손님 대화는 서버가 남기지 않는다.
import type { AskResponse, ConversationDetail, ConversationList, ConversationTurn } from "../api/types";
import { authFetch } from "../auth/client";

/** status 404: 지워졌거나 다른 계정의 대화, 401: 로그인이 풀림 */
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

/** 내 대화 목록 (최근에 대화한 것부터) */
export function listConversations(limit = 50): Promise<ConversationList> {
  return getJson(`/conversations?limit=${limit}`);
}

/** 대화 하나: 질문과 그때 보여 준 답 (다시 계산하지 않는다) */
export function getConversation(sessionId: string): Promise<ConversationDetail> {
  return getJson(`/conversations/${encodeURIComponent(sessionId)}`);
}

/** 목록·턴·이어 갈 조건을 함께 지운다. 이미 없으면(404) 지운 것으로 본다 */
export async function deleteConversation(sessionId: string): Promise<void> {
  const res = await authFetch(`/conversations/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  if (!res.ok && res.status !== 404) throw new HistoryError(res.status);
}

/** 남겨 둔 답 → 화면이 그리는 /ask 응답
 *  서버는 다시 보여 줄 칸만 남긴다: 계산 결과(result)·질문 분석(parsed)이 없어 보고서 버튼은 나오지 않는다.
 *  0019 전의 턴은 답 첫 부분(answer)만 있다 */
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

/** 목록에 적는 마지막 대화 시각: 오늘은 시각, 어제는 "어제", 올해는 월·일, 그 전은 연도까지 */
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
