import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { AskResponse } from "../api/types";
import { bundleBody } from "./report";
import { MAX_ITEMS, pickable, type Report } from "./reports";
import { ReportSheet } from "./ReportSheet";

const answer = (over: Partial<AskResponse>): AskResponse => ({
  status: "ok", answer: "2024년 부산광역시의 출생아 수는 13,063명입니다.\n\n- 계산", parsed: {}, result: {},
  sources: [], question: "2024년 부산 출생아 수", turn_id: 1, reportable: true, ...over,
});

describe("대화창 [보고서]: 이 대화의 답 고르기", () => {
  it("검증된 수치 답만 고를 수 있고, 못 고르는 답은 이유를 보여 준다", () => {
    expect(pickable(answer({}))).toEqual({
      turnId: 1, question: "2024년 부산 출생아 수", answer: "2024년 부산광역시의 출생아 수는 13,063명입니다.", reason: null,
    });
    expect(pickable(answer({ status: "tables", reportable: false })).reason).toBe("수치로 답하지 않은 답이에요");
    expect(pickable(answer({ request_type: "chart", reportable: false })).reason).toContain("차트·요약만");
    expect(pickable(answer({ reportable: false })).reason).toBe("검증된 수치로 답한 결과만 담을 수 있어요");
    expect(pickable(answer({ turn_id: null })).reason).toBe("저장된 답이 아니라 담을 수 없어요");
  });

  it("고르기 창은 이 대화의 답을 보여 주고, 못 고르는 답은 꺼 둔다", () => {
    const html = renderToStaticMarkup(
      <ReportSheet
        sessionId="A_1"
        answers={[answer({}), answer({ turn_id: 2, status: "tables", reportable: false, question: "표 안내" })]}
        onClose={vi.fn()}
      />,
    );
    expect(html).toContain("2024년 부산 출생아 수");
    expect(html).toContain("수치로 답하지 않은 답이에요");
    expect(html).toContain(`0/${MAX_ITEMS} 선택`);
    // 꺼 둔 답의 체크박스와 아직 고르지 않아 누를 수 없는 [보고서 만들기]
    expect(html.match(/disabled=""/g)).toHaveLength(2);
  });
});

describe("대화창 [보고서]: 고른 답으로 만든 보고서", () => {
  const report: Report = {
    id: 5, session_id: "A_1", title: "통계 분석 보고서", purpose: null, status: "ready", items: [{ turn_id: 1 }],
    content: {
      summary: [{ text: "2024년 부산광역시의 출생아 수는 13,063명이었습니다." }],
      analysis: [],
      limits: ["이 보고서는 통계 값만 정리하며, 값이 달라진 원인은 판단하지 않습니다."],
      items: [{ id: "r1", turn_id: 1, question: "<b>부산</b> 출생아", value_text: "13,063명", sources: [] }],
    },
  };

  it("요약 · 담은 결과 · 유의할 점을 싣고, 분석이 없으면 그 칸을 두지 않는다", () => {
    const html = bundleBody(report, new Date("2026-11-26T00:00:00Z"));
    expect(html).toContain("<h2>요약</h2>");
    expect(html).toContain("13,063명이었습니다");
    expect(html).toContain("<h2>담은 결과</h2>");
    expect(html).toContain("<h2>유의할 점</h2>");
    expect(html).not.toContain("<h2>분석</h2>");
    expect(html).not.toContain("<b>부산</b>"); // 질문 글은 HTML로 실행하지 않는다
  });

  it("분석이 있으면 싣는다", () => {
    const html = bundleBody(
      { ...report, content: { ...report.content, analysis: [{ text: "서울이 부산보다 많습니다." }] } },
      new Date(),
    );
    expect(html).toContain("<h2>분석</h2><ul><li>서울이 부산보다 많습니다.</li></ul>");
  });
});
