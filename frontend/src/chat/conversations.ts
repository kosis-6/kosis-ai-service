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

/** 로그인하지 않은 대화는 차트 보기를 저장할 곳이 없어, 마지막 차트의 보기를 질문과 함께 보낸다.
 *  서버는 직전 차트를 다시 쓰는 답('자세히 말해줘')을 이 모양으로 그린다 */
export function lastChartView(answers: AskResponse[]): ChartView | undefined {
  return answers.findLast((answer) => answer.chart)?.chart?.view ?? undefined;
}

/** 차트 보기 저장이 실패하면 이만큼(ms) 기다렸다가 다시 보낸다. 다시 보내지 않으면 화면은 바꾼
 *  모양인데 서버에는 이전 모양이 남아, 후속 답 · 다시 연 대화 · 말로 만든 보고서가 이전 모양으로 그렸다.
 *  질문은 저장이 끝나기를 기다리므로 다 합쳐 몇 초 안에 끝나게 둔다 */
export const CHART_VIEW_RETRY_MS = [500, 1000, 2000];

/** 다시 보내면 될 수 있는 실패만: 네트워크 오류(fetch가 던짐)와 서버 · 로그인 확인의 일시 오류.
 *  로그아웃(401) · 지운 대화(404) · 그릴 수 없는 모양(409)은 다시 보내도 같다 */
function retryable(error: unknown): boolean {
  if (!(error instanceof HistoryError)) return true;
  return error.status >= 500 || error.status === 408 || error.status === 429;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export function createChartViewQueue(persist = saveChartView, retryMs = CHART_VIEW_RETRY_MS) {
  const pending = new Map<string, Map<number, Promise<void>>>();
  const newest = new Map<string, number>();
  let order = 0;

  return {
    save(sessionId: string, turnId: number, view: ChartView): Promise<void> {
      const turns = pending.get(sessionId) ?? new Map<number, Promise<void>>();
      pending.set(sessionId, turns);
      const previous = turns.get(turnId);
      const key = `${sessionId}:${turnId}`;
      const mine = ++order;
      newest.set(key, mine);
      // 다시 보내기 전에 같은 답을 또 바꿨으면 그만둔다: 뒤의 저장이 마지막 모양을 남긴다
      const replaced = () => newest.get(key) !== mine;
      const write = async () => {
        for (let attempt = 0; ; attempt++) {
          try {
            await persist(sessionId, turnId, view);
            return;
          } catch (error) {
            if (attempt >= retryMs.length || !retryable(error) || replaced()) throw error;
            await sleep(retryMs[attempt]);
            if (replaced()) throw error;
          }
        }
      };
      const request = previous ? previous.then(write) : write();
      const settled = request.catch(() => {});
      turns.set(turnId, settled);
      void settled.then(() => {
        if (turns.get(turnId) === settled) turns.delete(turnId);
        if (turns.size === 0) pending.delete(sessionId);
        if (newest.get(key) === mine) newest.delete(key);
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
