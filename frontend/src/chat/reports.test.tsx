import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { AskResponse } from "../api/types";
import { bundleBody, reportDocument } from "./report";
import { MAX_ITEMS, pickable, type Report } from "./reports";
import { ReportPreview, ReportSheet } from "./ReportSheet";

const answer = (over: Partial<AskResponse>): AskResponse => ({
  status: "ok", answer: "2024년 부산광역시의 출생아 수는 13,063명입니다.\n\n- 계산", parsed: {}, result: {},
  sources: [], question: "2024년 부산 출생아 수", turn_id: 1, reportable: true, ...over,
});

describe("대화창 [보고서]: 이 대화의 답 고르기", () => {
  it("검증된 수치로 답한 저장된 답만 고를 수 있다 (요약 · 차트 다시 보기 · 표 안내 · 실패 · 저장 안 된 답은 빠진다)", () => {
    expect(pickable([
      answer({}),
      answer({ turn_id: 2, question: "요약해줘", request_type: "summary", reportable: false }),
      answer({ turn_id: 3, request_type: "chart", reportable: false }),
      answer({ turn_id: 4, status: "tables", reportable: false }),
      answer({ turn_id: 5, status: "missing_data", reportable: false }),
      answer({ turn_id: null }),
    ])).toEqual([
      { turnId: 1, question: "2024년 부산 출생아 수", answer: "2024년 부산광역시의 출생아 수는 13,063명입니다." },
    ]);
  });

  it("고르기 창에는 고를 수 있는 답만 보여 준다 (고를 수 없는 답을 꺼 둔 채 보여 주지 않는다)", () => {
    const html = renderToStaticMarkup(
      <ReportSheet
        sessionId="A_1"
        answers={[answer({}), answer({ turn_id: 2, request_type: "summary", reportable: false, question: "요약해줘" })]}
        onClose={vi.fn()}
      />,
    );
    expect(html).toContain("2024년 부산 출생아 수");
    expect(html).not.toContain("요약해줘");
    expect(html).toContain("요약은 보고서가 고른 답으로 직접 만들어요");
    expect(html).toContain(`0/${MAX_ITEMS} 선택`);
    expect(html).toContain(`1개부터 ${MAX_ITEMS}개까지`);
    // 아직 고르지 않아 누를 수 없는 [보고서 만들기]만 꺼져 있다
    expect(html.match(/disabled=""/g)).toHaveLength(1);
  });

  it("담을 답이 하나도 없으면 목록 · 만들기 버튼 없이 안내만 보여 준다", () => {
    const html = renderToStaticMarkup(
      <ReportSheet sessionId="A_1" answers={[answer({ request_type: "summary", reportable: false })]} onClose={vi.fn()} />,
    );
    expect(html).toContain("보고서에 담을 답이 아직 없어요");
    expect(html).not.toContain("report-picks");
    expect(html).not.toContain("보고서 만들기</button>");
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

  it("머리글 · 핵심 수치 · 요약 · 결과별 내용 · 유의할 점을 싣고, 분석이 없으면 그 칸을 두지 않는다", () => {
    const html = bundleBody(report, new Date("2026-11-26T00:00:00Z"));
    expect(html).toContain("<strong>LIKE SNAKE</strong>인구 통계 안내");
    expect(html).toContain("결과 1개 · 2026년 11월 26일");
    expect(html).toContain('<dl class="figures"><div><dt>&lt;b&gt;부산&lt;/b&gt; 출생아</dt><dd>13,063명</dd></div></dl>');
    expect(html).toContain("<h2>요약</h2>");
    expect(html).toContain("13,063명이었습니다");
    expect(html).toContain("<h2>결과별 내용</h2>");
    expect(html).toContain("<h2>유의할 점</h2>");
    expect(html).not.toContain("<h2>분석</h2>");
    expect(html).not.toContain("<b>부산</b>"); // 질문 글은 HTML로 실행하지 않는다
  });

  it("분석이 있으면 싣는다", () => {
    const html = bundleBody(
      { ...report, content: { ...report.content, analysis: [{ text: "서울이 부산보다 많습니다." }] } },
      new Date(),
    );
    expect(html).toContain('<h2>분석</h2><ul class="lines"><li>서울이 부산보다 많습니다.</li></ul>');
  });

  it("결과마다 적용 조건 · 수치 표를 싣고, 핵심 수치는 추이면 마지막 해 · 순위면 1위", () => {
    const series = {
      kind: "series", label: "부산 출생아 수", unit: "명", value: null,
      rows: [{ label: "2023년", value: 13063 }, { label: "2024년", value: 14010 }],
    };
    const ranking = {
      kind: "ranking", label: "2024년 시도별 출생아 수", unit: "명", value: null,
      rows: [{ label: "경기도", value: 71285, rank: 1 }, { label: "서울특별시", value: 41605, rank: 2 }],
    };
    const html = bundleBody({
      ...report,
      items: [
        { turn_id: 1, snapshot: { applied: [{ name: "지역", value: "부산", source: "user" }, { name: "성별", value: "계", source: "default" }] } },
        { turn_id: 2, snapshot: {} },
      ],
      content: {
        ...report.content,
        items: [
          { id: "r1", turn_id: 1, question: "부산 출생아 추이", figure: series, value_text: null, sources: [] },
          { id: "r2", turn_id: 2, question: "시도별 순위", figure: ranking, value_text: null, sources: [] },
        ],
      },
    }, new Date());
    expect(html).toContain("<dd>2024년 14,010명</dd>");
    expect(html).toContain("<dd>1위 경기도 71,285명</dd>");
    expect(html).toContain('<ul class="conditions" aria-label="적용한 조건"><li><span>지역</span>부산</li><li><span>성별</span>계<em>기본값</em></li></ul>');
    expect(html).toContain('<th>연도</th><th class="num">값 (명)</th>');
    expect(html).toContain('<td>2023년</td><td class="num">13,063</td>');
    expect(html).toContain('<th>순위</th><th>지역</th><th class="num">값 (명)</th>');
    expect(html).toContain('<td>1</td><td>경기도</td><td class="num">71,285</td>');
  });
});

describe("대화창 [보고서]: 만든 보고서 미리보기 · PDF", () => {
  const report: Report = {
    id: 5, session_id: "A_1", title: "통계 분석 보고서", purpose: null, status: "ready", items: [{ turn_id: 1 }],
    content: { summary: [{ text: "요약" }], items: [{ id: "r1", turn_id: 1, question: "부산 출생아", value_text: "1명" }] },
  };
  const at = new Date("2026-11-27T01:00:00Z");

  it("미리보기 문서는 스타일을 파일로 불러오고 스크립트 · 문서 안 스타일이 없다 (페이지 보안 정책)", () => {
    const doc = reportDocument(report, at, "/chat/assets/report.css");
    expect(doc).toContain('<link rel="stylesheet" href="/chat/assets/report.css">');
    expect(doc).toContain('<base target="_blank">'); // 출처 링크는 새 탭으로
    expect(doc).toContain("<title>통계 분석 보고서 2026-11-27</title>"); // 'PDF로 저장' 파일 이름
    for (const bad of ["<script", "<style", 'style="', "onclick="]) expect(doc).not.toContain(bad);
  });

  it("미리보기는 앱 안에서 먼저 보여 주고, 스크립트를 막은 채 PDF로 저장 · 답 다시 고르기를 둔다", () => {
    const html = renderToStaticMarkup(<ReportPreview report={report} onBack={vi.fn()} />);
    expect(html).toContain('title="보고서 미리보기"');
    expect(html).toContain('sandbox="allow-same-origin allow-modals allow-popups allow-popups-to-escape-sandbox"');
    expect(html).not.toContain("allow-scripts");
    expect(html).toContain("통계 분석 보고서"); // 보고서 내용이 미리보기 문서에 들어 있다
    expect(html).toContain("PDF로 저장");
    expect(html).toContain("답 다시 고르기");
    expect(html).not.toContain("HTML");
  });
});
