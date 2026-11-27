import { describe, expect, it } from "vitest";
import { bundleBody, chartSvg } from "./report";
import type { Report } from "./reports";

const report = (over: Partial<NonNullable<Report["content"]>> = {}): Report => ({
  id: 1, session_id: "A_1", title: "통계 분석 보고서", purpose: null, status: "ready", items: [{ turn_id: 1 }],
  content: {
    summary: [{ text: "요약" }], analysis: [], limits: [],
    items: [{ id: "r1", turn_id: 1, question: "서울 출생아 수", value_text: "12,345명", sources: [] }],
    ...over,
  },
});

describe("보고서 화면 안전", () => {
  it("외부 입력을 HTML로 실행하지 않고 위험한 출처 링크를 제외한다", () => {
    const html = bundleBody(report({
      summary: [{ text: "<script>alert(1)</script>" }],
      items: [{
        id: "r1", turn_id: 1, question: '<img src=x onerror="alert(1)">', value_text: "1명",
        sources: [{ name: "<악성>", basis: '"기준"', url: "javascript:alert(1)" }],
      }],
    }), new Date());
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("javascript:");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("&lt;악성&gt;");
  });

  it("안전한 출처는 통계표 링크를 단다", () => {
    const html = bundleBody(report({
      items: [{
        id: "r1", turn_id: 1, question: "q", value_text: "1명",
        sources: [{ name: "통계표", basis: "2024년", url: "https://kosis.kr/table" }],
      }],
    }), new Date());
    expect(html).toContain('<a href="https://kosis.kr/table" rel="noopener noreferrer">통계표 보기</a>');
  });
});

describe("보고서 차트", () => {
  it("추이 그래프에서 결측 구간의 선을 연결하지 않는다", () => {
    const svg = chartSvg({
      type: "line", title: "추이", unit: "명", category: "연도",
      rows: [{ label: "2020", value: 10 }, { label: "2021", value: null }, { label: "2022", value: 30 }],
    });
    expect(svg.match(/<polyline /g)).toHaveLength(2);
    expect(svg).not.toContain("NaN");
  });

  it("막대 보기로 요청한 추이를 막대로 보존하고, 음수도 너비가 음수가 되지 않는다", () => {
    const svg = chartSvg({
      type: "line", view: "bar", title: "추이", unit: "명", category: "연도",
      rows: [{ label: "2020", value: 10 }, { label: "2021", value: null }, { label: "2022", value: -5 }],
    });
    expect(svg.match(/<rect /g)).toHaveLength(2);
    expect(svg).not.toContain("<polyline ");
    expect(svg).not.toMatch(/(width|height)="-/);
  });

  it("표로 보기로 요청한 차트는 그리지 않는다 (수치 표가 대신한다)", () => {
    expect(chartSvg({ type: "bar", view: "table", title: "t", unit: "명", category: "지역", rows: [{ label: "서울", value: 1 }] })).toBe("");
  });

  it("지역이 선 색보다 많으면 지역마다 작은 그래프로 그린다 (색을 돌려 쓰지 않는다)", () => {
    const series = Array.from({ length: 8 }, (_, i) => `지역${i}`);
    const svg = chartSvg({
      type: "line", title: "인구", unit: "명", category: "연도", series,
      rows: [2023, 2024].map((y) => ({ label: String(y), value: null, values: series.map((_, i) => y + i) })),
    });
    expect(svg.match(/class="report-cell"/g)).toHaveLength(8);
    expect(svg).toContain("세로축은 지역마다");
  });

  it("비교 · 순위 막대는 한 색, 구성비 조각만 색을 나눈다 (화면 차트와 같음)", () => {
    const rows = [{ label: "가", value: 1 }, { label: "나", value: 2 }];
    const bars = chartSvg({ type: "bar", title: "t", unit: "명", category: "지역", rows });
    expect(new Set(bars.match(/<rect [^>]*fill="([^"]+)"/g)?.map((m) => m.match(/fill="([^"]+)"/)![1])).size).toBe(1);
    const parts = chartSvg({ type: "parts", title: "t", unit: "%", category: "항목", rows });
    expect(new Set(parts.match(/<rect [^>]*fill="([^"]+)"/g)?.map((m) => m.match(/fill="([^"]+)"/)![1])).size).toBe(2);
  });
});
