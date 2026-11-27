import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { pickable } from "./reports";
import { Answer } from "./Answer";
import { createChartViewQueue, replayed, saveChartView, when } from "./conversations";

const calls: { url: string; init?: RequestInit }[] = [];
vi.mock("../auth/client", () => ({
  authFetch: async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(null, { status: url.includes("/turns/9/") ? 409 : 204 });
  },
}));

const turn = (response: Record<string, unknown>) => ({
  turn_no: 1,
  question: "서울 인구 알려줘",
  created_at: "2026-11-19T01:00:00Z",
  response,
});

describe("지난 대화 다시 그리기", () => {
  it("남겨 둔 답에 화면이 읽는 칸을 채운다", () => {
    const data = replayed(turn({ status: "ok", answer: "서울 인구는 ...", sources: [{ name: "표" }] }), "A_1");
    expect(data).toMatchObject({ status: "ok", answer: "서울 인구는 ...", session_id: "A_1", question: "서울 인구 알려줘" });
    expect(data.parsed).toEqual({});
    expect(data.result).toEqual({});
    expect(data.sources).toHaveLength(1);
  });

  it("답 첫 부분만 남은 예전 턴(0019 전)도 그린다", () => {
    const data = replayed(turn({ answer: "예전 답" }), "A_1");
    expect(data.sources).toEqual([]);
    const html = renderToStaticMarkup(
      createElement(Answer, { data, busy: false, onSend: () => {}, onOthers: () => {}, onAskAgain: () => {} }),
    );
    expect(html).toContain("예전 답");
  });

  it("다시 연 대화의 답도 보고서에 고를 수 있다 (서버가 담을 수 있는 답에 turn_id를 붙인다)", () => {
    const saved = { status: "ok", answer: "답", sources: [], reportable: true, turn_id: 7 };
    expect(pickable([replayed(turn(saved), "A_1")])).toMatchObject([{ turnId: 7 }]);
    expect(pickable([replayed(turn({ status: "ok", answer: "답", sources: [] }), "A_1")])).toEqual([]);
  });
});

describe("목록의 마지막 대화 시각", () => {
  const now = new Date(2026, 10, 19, 18, 30);
  it("오늘은 시각, 어제는 '어제'", () => {
    expect(when(new Date(2026, 10, 19, 9, 5).toISOString(), now)).toBe("09:05");
    expect(when(new Date(2026, 10, 18, 23, 59).toISOString(), now)).toBe("어제");
  });
  it("올해는 월·일, 그 전은 연도까지", () => {
    expect(when(new Date(2026, 0, 3).toISOString(), now)).toBe("1월 3일");
    expect(when(new Date(2025, 11, 31).toISOString(), now)).toBe("2025. 12. 31.");
  });
  it("읽을 수 없는 시각은 비운다", () => {
    expect(when("없음", now)).toBe("");
  });
});

describe("화면에서 바꾼 차트 모양 남기기", () => {
  it("그 답의 모양을 서버에 남긴다 (말로 만든 보고서 · 다시 연 대화가 쓴다)", async () => {
    await saveChartView("A_1", 7, "bar");
    expect(calls.at(-1)?.url).toBe("/conversations/A_1/turns/7/chart-view");
    expect(calls.at(-1)?.init?.method).toBe("PUT");
    expect(JSON.parse(String(calls.at(-1)?.init?.body))).toEqual({ view: "bar" });
  });

  it("서버가 받지 않으면 알린다", async () => {
    await expect(saveChartView("A_1", 9, "bar")).rejects.toThrow();
  });

  it.each([false, true])("느린 첫 저장이 마지막 선택을 덮어쓰지 않는다 (첫 저장 실패: %s)", async (fail) => {
    let finishFirst!: () => void;
    const first = new Promise<void>((resolve, reject) => {
      finishFirst = () => fail ? reject(new Error("network")) : resolve();
    });
    const persist = vi.fn<typeof saveChartView>().mockResolvedValue(undefined).mockReturnValueOnce(first);
    const queue = createChartViewQueue(persist);
    const bar = queue.save("A_1", 7, "bar").catch(() => {});
    const table = queue.save("A_1", 7, "table");
    const complete = vi.fn();
    const waiting = queue.wait("A_1").then(complete);
    await queue.wait("other");
    expect(persist.mock.calls).toEqual([["A_1", 7, "bar"]]);
    expect(complete).not.toHaveBeenCalled();

    finishFirst();
    await Promise.all([bar, table, waiting]);
    expect(persist.mock.calls).toEqual([["A_1", 7, "bar"], ["A_1", 7, "table"]]);
    expect(complete).toHaveBeenCalledOnce();
  });
});

