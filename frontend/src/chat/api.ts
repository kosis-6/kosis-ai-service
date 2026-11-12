import type { SearchRequest, SearchResponse } from "../api/types";
import { authFetch } from "../auth/client";

/** POST /search. 목업 모드면 서버·HCX 를 부르지 않고 mock.js 가 응답을 만든다 */
export async function search(body: SearchRequest, mock: boolean): Promise<SearchResponse> {
  if (mock) {
    const { MockApi } = await import("./mock.js"); // 목업을 켰을 때만 내려받는다
    return MockApi.search(body);
  }
  // 로그인했으면 토큰을 붙여 보낸다 (검색 기록이 사용자와 이어진다). 만료되면 새로 받아 다시 보낸다
  const res = await authFetch("/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = JSON.stringify((await res.json()).detail);
    } catch {
      /* 본문 없음 */
    }
    throw new Error(`서버 오류 ${res.status}${detail ? `: ${detail}` : ""}`);
  }
  return (await res.json()) as SearchResponse;
}
