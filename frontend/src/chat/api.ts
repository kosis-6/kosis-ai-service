import type { AskRequest, AskResponse, SearchRequest, SearchResponse } from "../api/types";
import { authFetch } from "../auth/client";

// 로그인했으면 토큰을 붙여 보낸다 (기록이 사용자와 이어진다). 만료되면 새로 받아 다시 보낸다
async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* 본문 없음 */
    }
    // 서버가 직접 쓴 안내(요청이 많음, 자료 준비 중)는 그대로 보여 준다
    if (typeof detail === "string") throw new Error(detail);
    throw new Error(`서버 오류 ${res.status}${detail ? `: ${JSON.stringify(detail)}` : ""}`);
  }
  return (await res.json()) as T;
}

/** POST /search (통계표 찾기). 목업 모드면 서버·HCX 를 부르지 않고 mock.js 가 응답을 만든다 */
export async function search(body: SearchRequest, mock: boolean): Promise<SearchResponse> {
  if (mock) {
    const { MockApi } = await import("./mock.js"); // 목업을 켰을 때만 내려받는다
    return MockApi.search(body);
  }
  return postJson<SearchResponse>("/search", body);
}

/** POST /ask (수치 질문). 목업 모드면 mockAsk.ts 의 저장된 답을 돌려준다 */
export async function ask(body: AskRequest, mock: boolean): Promise<AskResponse> {
  if (mock) {
    const { MockAsk } = await import("./mockAsk");
    return MockAsk.ask(body);
  }
  return postJson<AskResponse>("/ask", body);
}
