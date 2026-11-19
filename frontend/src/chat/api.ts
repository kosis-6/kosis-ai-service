import type { AskRequest, AskResponse } from "../api/types";
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

/** POST /ask: 질문 하나에 수치 답 또는 통계표 안내 */
export function ask(body: AskRequest): Promise<AskResponse> {
  return postJson<AskResponse>("/ask", body);
}
