import { describe, expect, it } from "vitest";
import type { AskResponse } from "../api/types";
import { buildPreview, buildReport, canReport, reportFileName, reportTables } from "./report";

const base: AskResponse = {
  status: "ok", question: "서울 출생아 수", answer: "서울의 출생아 수입니다.\n\n※ 계산 기준",
  parsed: {}, sources: [{ name: "통계표", basis: "2024년", url: "https://kosis.kr/table" }],
  applied: [{ name: "지역", value: "서울", source: "user" }],
  result: { status: "ok", indicator: "출생아 수", region: "서울", year: 2024, unit: "명", value: 12345 },
};

describe("분석 보고서", () => {
  it("결과 전체를 표로 보존하고 결측을 0으로 바꾸지 않는다", () => {
    const ranking = Array.from({ length: 25 }, (_, i) => ({ region: `지역${i}`, rank: i + 1, value: i ? i : null }));
    const data = { ...base, result: { status: "ok", unit: "명", ranking }, chart: {
      type: "bar" as const, title: "순위", category: "지역", unit: "명",
      rows: ranking.slice(0, 15).map((r) => ({ label: r.region, value: r.value })),
      note: "25개 지역 중 15개만 표시",
    } };
    const html = buildReport(data, new Date("2026-11-14T00:00:00Z"));
    expect(html).toContain("지역24");
    expect(html).toContain("자료 없음");
    expect(html).toContain("25개 지역 중 15개만 표시");
    expect(html).toContain("https://kosis.kr/table");
    expect(html).toContain("2026");
    expect(reportTables(data.result)[0].rows).toHaveLength(25);
  });

  it("수치 표가 없는 결과(빈 결과·실패만)에서는 보고서를 만들지 않는다", () => {
    expect(canReport({ ...base, result: {} })).toBe(false);
    expect(canReport({ ...base, result: { status: "ok", split: [{ status: "no_data", reason: "없음" }] } })).toBe(false);
    expect(canReport(base)).toBe(true);
  });

  it("연도·순위는 쉼표 없이, 구성비 원래 값 단위가 없으면 명으로 쓴다", () => {
    const series = [{ year: 2024, value: 9331828 }];
    const trend = buildReport({ ...base, result: { status: "ok", unit: "명", series } });
    expect(trend).toContain("<td>2024</td>");
    expect(trend).not.toContain("2,024");
    expect(trend).toContain("<td>9,331,828</td>");
    const parts = [{ label: "가", count: 1000, share: 10 }, { label: "나", count: 9000, share: 90 }];
    const composition = buildReport({ ...base, result: { status: "ok", unit: "%", parts } });
    expect(composition).toContain("원래 값 (명)");
  });

  it("추이 그래프에서 결측 구간의 선을 연결하지 않는다", () => {
    const html = buildReport({ ...base, chart: {
      type: "line", title: "추이", unit: "명", category: "연도",
      rows: [{ label: "2020", value: 10 }, { label: "2021", value: null }, { label: "2022", value: 30 }],
    } });
    expect(html.match(/<polyline /g)).toHaveLength(2);
    expect(html).not.toContain("NaN");
  });

  it("구성비 원값·비중과 여러 지표별 단위를 보존한다", () => {
    const result = { status: "ok", split: [
      { status: "ok", indicator: "구성", unit: "%", count_unit: "명", parts: [
        { label: "가", count: 10, share: 10 }, { label: "나", count: 70, share: 70 },
      ] },
      { status: "ok", indicator: "다른 지표", unit: "건", value: 2 },
    ] };
    const tables = reportTables(result);
    expect(tables[0].rows).toEqual([["가", 10, 10], ["나", 70, 70]]);
    expect(tables[1].title).toContain("건");
    expect(buildReport({ ...base, result })).toContain("원래 값 (명)");
  });

  it("외부 입력을 HTML로 실행하지 않고 위험한 출처 링크를 제외한다", () => {
    const html = buildReport({ ...base, question: '<img src=x onerror="alert(1)">',
      answer: "<script>alert(1)</script>",
      sources: [{ name: "<악성>", basis: '"기준"', url: "javascript:alert(1)" }],
    });
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("javascript:");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("&lt;악성&gt;");
  });

  it("실패·표 안내·차트 모양만 바꾼 답에서 보고서를 만들지 않는다", () => {
    for (const data of [{ ...base, status: "tables" }, { ...base, status: "missing_data" },
      { ...base, result: { status: "ok", reused: true } }]) {
      expect(canReport(data)).toBe(false);
      expect(() => buildReport(data)).toThrow();
    }
  });

  it("음수 값·합계 세부 지역을 유지하고 전달받은 결과를 바꾸지 않는다", () => {
    const data = { ...base, result: { status: "ok", value: -50, members: [
      { region: "서울", value: -100 }, { region: "부산", value: 50 },
    ] }, chart: { type: "bar" as const, title: "순이동", unit: "명", category: "지역",
      rows: [{ label: "서울", value: -100 }, { label: "부산", value: 50 }] } };
    const before = JSON.stringify(data);
    const html = buildReport(data);
    expect(reportTables(data.result)[0].rows).toEqual([["서울", -100], ["부산", 50], ["합계", -50]]);
    expect(html).not.toMatch(/width="-/);
    expect(JSON.stringify(data)).toBe(before);
  });
  it("미리보기는 인라인 스타일·스크립트 없이 파일로 불러오고, 저장할 파일을 그대로 담는다", () => {
    const at = new Date("2026-11-14T00:00:00Z");
    const data = { ...base, answer: "</script><script>alert(1)</script>" };
    const assets = { css: "http://localhost/chat/assets/report.css", script: "http://localhost/chat/assets/report-view.js" };
    const preview = buildPreview(data, assets, at);
    // 채팅 화면 CSP(script-src·style-src 'self')에서 막히는 것이 없어야 한다 (저장할 파일 JSON은 데이터라 제외)
    const markup = preview.replace(/<script type="application\/json"[\s\S]*?<\/script>/, "");
    expect(markup).not.toMatch(/<style|onclick=|style="/);
    expect(preview).toContain('<link rel="stylesheet" href="http://localhost/chat/assets/report.css">');
    expect(preview).toContain('<script src="http://localhost/chat/assets/report-view.js"></script>');
    expect(preview).toContain('id="report-save"');
    // 저장할 파일: JSON 안의 </script>가 미리보기의 script 태그를 닫지 않는다
    const json = preview.match(/<script type="application\/json" id="report-file"[^>]*>([\s\S]*?)<\/script>/)![1];
    expect(json).not.toContain("<");
    expect(JSON.parse(json)).toBe(buildReport(data, at));
    expect(preview).toContain(`data-filename="${reportFileName(at)}"`);
    expect(reportFileName(at)).toBe("통계보고서-2026-11-14.html");
    // 저장 파일은 혼자 열려야 하므로 스타일을 안에 담는다
    expect(buildReport(data, at)).toContain("<style>");
  });
  it("지역이 선 색보다 많으면 지역마다 작은 그래프로 그린다 (색을 돌려 쓰지 않는다)", () => {
    const series = Array.from({ length: 8 }, (_, i) => `지역${i}`);
    const chart = {
      type: "line" as const, title: "인구", unit: "명", category: "연도", series,
      rows: [2023, 2024].map((y) => ({ label: String(y), value: null, values: series.map((_, i) => y + i) })),
    };
    const html = buildReport({ ...base, chart });
    expect(html.match(/class="report-cell"/g)).toHaveLength(8);
    expect(html).toContain("세로축은 지역마다");
  });
});
