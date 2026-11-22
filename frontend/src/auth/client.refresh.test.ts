// Access Token 이 만료됐을 때 다시 보내기 (client.ts 의 call)
//   - 요청 여러 개가 함께 401 을 받아도 새 토큰은 한 번만 받고, 모두 새 토큰으로 다시 보낸다
//   - 새 토큰을 받지 못하면(서버 오류) 로그인 없이(손님으로) 다시 보내지 않는다
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Call = { url: string; auth: string | null };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

// 서버 흉내: 지금 유효한 토큰만 받는다. refresh 는 refreshStatus 로 답한다
function server(refreshStatus: number) {
  const calls: Call[] = [];
  let valid = "old";
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const auth = new Headers(init?.headers).get("Authorization");
    calls.push({ url, auth });
    if (url === "/auth/token") return json(200, { access_token: "old", token_type: "bearer" });
    if (url === "/auth/me") return json(200, { id: 1, email: "a@b.co", nickname: "통계왕", is_admin: false });
    if (url === "/auth/refresh") {
      await new Promise((r) => setTimeout(r, 5)); // 다른 요청의 401 이 그사이 도착한다
      if (refreshStatus !== 200) return json(refreshStatus, { detail: "잠시 뒤에 다시 시도해 주세요." });
      valid = "new";
      return json(200, { access_token: "new", token_type: "bearer" });
    }
    if (auth === null) return json(200, { as: "guest" });
    return auth === `Bearer ${valid}` ? json(200, { as: "user" }) : json(401, { detail: "로그인이 필요합니다." });
  });
  return { calls, fetchMock, expire: () => (valid = "expired") };
}

async function signedIn(refreshStatus: number) {
  vi.resetModules();
  const s = server(refreshStatus);
  vi.stubGlobal("fetch", s.fetchMock);
  const client = await import("./client");
  await client.login("a@b.co", "pw1234!", false);
  s.expire(); // 이제 "old" 토큰은 401
  return { ...s, client };
}

describe("토큰 만료 뒤 다시 보내기", () => {
  beforeEach(() => vi.useRealTimers());
  afterEach(() => vi.unstubAllGlobals());

  it("두 요청이 함께 401 을 받아도 새 토큰을 한 번만 받고 둘 다 성공한다", async () => {
    const { client, calls } = await signedIn(200);
    const [a, b] = await Promise.all([client.authFetch("/ask"), client.authFetch("/ask")]);
    expect([a.status, b.status]).toEqual([200, 200]);
    expect(calls.filter((c) => c.url === "/auth/refresh")).toHaveLength(1);
  });

  it("새 토큰을 못 받으면(503) 손님으로 다시 보내지 않는다", async () => {
    const { client, calls } = await signedIn(503);
    const res = await client.authFetch("/ask");
    expect(res.status).toBe(401);
    expect(calls.filter((c) => c.url === "/ask" && c.auth === null)).toHaveLength(0);
    expect(client.currentUser()).not.toBeNull(); // 서버 오류라 로그인 표시는 남긴다 (다시 시도할 수 있게)
  });
});
