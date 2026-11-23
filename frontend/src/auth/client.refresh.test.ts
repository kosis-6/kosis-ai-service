// Access Token 이 만료됐을 때 다시 보내기 (client.ts 의 call)
//   - 요청 여러 개가 함께 401 을 받아도 새 토큰은 한 번만 받고, 모두 새 토큰으로 다시 보낸다
//   - 갱신 중 새 요청과 갱신 실패 뒤 다시 보낸 요청도 인증을 유지한다
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Call = { url: string; auth: string | null };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function server(...refreshStatuses: number[]) {
  const calls: Call[] = [];
  let valid = "old";
  let refreshGate: Promise<void> | undefined;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const auth = new Headers(init?.headers).get("Authorization");
    calls.push({ url, auth });
    if (url === "/auth/token") return json(200, { access_token: "old", token_type: "bearer" });
    if (url === "/auth/me") return json(200, { id: 1, email: "a@b.co", nickname: "통계왕", is_admin: false });
    if (url === "/auth/logout") return new Response(null, { status: 204 });
    if (url === "/auth/refresh") {
      await refreshGate;
      const refreshStatus = refreshStatuses.shift() ?? 200;
      if (refreshStatus !== 200) return json(refreshStatus, { detail: "잠시 뒤에 다시 시도해 주세요." });
      valid = "new";
      return json(200, { access_token: "new", token_type: "bearer" });
    }
    if (auth === null) return json(200, { as: "guest" });
    return auth === `Bearer ${valid}` ? json(200, { as: "user" }) : json(401, { detail: "로그인이 필요합니다." });
  });
  return {
    calls,
    fetchMock,
    expire: () => (valid = "expired"),
    pauseRefresh: () => {
      let resume!: () => void;
      refreshGate = new Promise<void>((resolve) => {
        resume = resolve;
      });
      return resume;
    },
  };
}

async function signedIn(...refreshStatuses: number[]) {
  vi.resetModules();
  const s = server(...refreshStatuses);
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

  it("갱신 중 시작된 질문은 같은 갱신을 기다리고 새 토큰을 붙인다", async () => {
    const { client, calls, pauseRefresh } = await signedIn(200);
    const resume = pauseRefresh();
    const first = client.authFetch("/ask");
    await vi.waitFor(() => expect(calls.some((c) => c.url === "/auth/refresh")).toBe(true));
    const second = client.authFetch("/ask");
    resume();
    expect((await Promise.all([first, second])).map((res) => res.status)).toEqual([200, 200]);
    expect(calls.filter((c) => c.url === "/auth/refresh")).toHaveLength(1);
    expect(calls.filter((c) => c.url === "/ask").map((c) => c.auth)).toEqual([
      "Bearer old", "Bearer new", "Bearer new",
    ]);
  });

  it("일시 오류가 계속되면 질문을 보류하고 갱신에 성공한 뒤 다시 보낸다", async () => {
    const { client, calls } = await signedIn(503, 503, 200);
    expect((await client.authFetch("/ask")).status).toBe(401);
    expect((await client.authFetch("/ask")).status).toBe(503);
    expect((await client.authFetch("/ask")).status).toBe(200);
    expect(calls.filter((c) => c.url === "/auth/refresh")).toHaveLength(3);
    expect(calls.filter((c) => c.url === "/ask").map((c) => c.auth)).toEqual(["Bearer old", "Bearer new"]);
  });

  it("로그아웃한 뒤 도착한 갱신 응답은 이전 질문을 다시 보내지 않는다", async () => {
    const { client, calls, pauseRefresh } = await signedIn(200);
    const resume = pauseRefresh();
    const first = client.authFetch("/ask");
    await vi.waitFor(() => expect(calls.some((c) => c.url === "/auth/refresh")).toBe(true));
    await client.logout();
    resume();
    expect((await first).status).toBe(401);
    const guest = await client.authFetch("/ask");
    expect(await guest.json()).toEqual({ as: "guest" });
    expect(calls.filter((c) => c.url === "/ask").map((c) => c.auth)).toEqual(["Bearer old", null]);
  });

  it("첫 화면에서 로그인 확인 중 시작한 질문은 세션이 만료돼도 손님 요청으로 바뀌지 않는다", async () => {
    vi.resetModules();
    const { fetchMock, calls, pauseRefresh } = server(401);
    const resume = pauseRefresh();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("localStorage", {
      getItem: () => JSON.stringify({ name: "통계왕", admin: false }),
      removeItem: vi.fn(),
    });
    const client = await import("./client");
    const ask = client.authFetch("/ask");
    resume();
    expect((await ask).status).toBe(401);
    expect(client.currentUser()).toBeNull();
    expect(calls.map((c) => c.url)).toEqual(["/auth/refresh"]);
  });
});
