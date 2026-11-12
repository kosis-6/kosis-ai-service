import { describe, expect, it } from "vitest";
import type { Recommendation, SearchResponse } from "../api/types";
import {
  bubbleText,
  checkLabel,
  defaultOption,
  memberAnswer,
  metaLine,
  noticeLines,
  otherVersions,
  pathText,
  safeUrl,
  splitLinks,
} from "./format";

const rec: Recommendation = {
  tbl_id: "DT_1",
  tbl_nm: "시도/출생장소별 출생",
  org_nm: "국가데이터처",
  survey_nm: "인구동향조사",
  period: "1991~2025",
  path: "주제별통계 > 인구 > 인구동향조사 > 시도/출생장소별 출생",
  url: "https://kosis.kr/x",
};

function response(extra: Partial<SearchResponse>): SearchResponse {
  return {
    session_id: "s",
    status: "ok",
    answer: "",
    parsed: {},
    clarify: null,
    recommendations: [],
    notice: null,
    ...extra,
  };
}

describe("표 카드 글자", () => {
  it("기관 · 조사 · 기간을 잇는다", () => {
    expect(metaLine(rec, " · ")).toBe("국가데이터처 · 인구동향조사 · 1991~2025년");
  });
  it("경로 끝이 표 이름이면 뺀다", () => {
    expect(pathText(rec)).toBe("주제별통계  ›  인구  ›  인구동향조사");
    expect(pathText({ ...rec, path: null })).toBeNull();
  });
  it("조건 이름: 질문의 연도·지역 수준을 그대로 보여 준다", () => {
    const parsed = { conditions: { year: 2020, year_end: 2024, region_level: "시군구" } };
    expect(checkLabel("year", parsed)).toBe("2020~2024년");
    expect(checkLabel("region_level", parsed)).toBe("시군구");
    expect(checkLabel("sex")).toBe("성별");
    expect(checkLabel("target:혼인 귀화자")).toBe("혼인 귀화자");
  });
  it("다른 버전은 10개까지만 적는다", () => {
    expect(otherVersions({})).toBeNull();
    const editions = Array.from({ length: 12 }, (_, i) => ({ period: String(2000 + i) }));
    expect(otherVersions({ members: [{ label: "남" }], editions })).toMatch(/^같은 표의 다른 버전: 남, 2000년판.* 외 3개$/);
  });
});

describe("말풍선", () => {
  it("카드로 보여 주는 줄은 뺀다", () => {
    const answer = "요청하신 통계는 인구동향조사에서 확인할 수 있습니다.\n추천 통계표: A\n바로가기: https://kosis.kr/x";
    expect(bubbleText(response({ answer, recommendations: [rec] }))).toBe(
      "요청하신 통계는 인구동향조사에서 확인할 수 있습니다.\n아래 통계표에서 자세한 자료를 살펴보세요.",
    );
  });
  it("되묻기는 첫 줄만, 추천이 없으면 그대로", () => {
    const clarify = { level: "member" as const, axes: [] };
    expect(bubbleText(response({ answer: "어느 대상인가요?\n1. 전체", clarify }))).toBe("어느 대상인가요?");
    expect(bubbleText(response({ answer: "범위 밖입니다.\n예) ..." }))).toBe("범위 밖입니다.\n예) ...");
  });
  it("부분 일치 알림에는 확인 안내를 붙인다", () => {
    expect(noticeLines(response({ status: "partial_match", notice: "연도 불충족; 성별 불충족" }))).toBe(
      "연도 불충족\n성별 불충족\n표의 제공 기간과 조건을 확인해 주세요.",
    );
  });
  it("주소만 링크로 나눈다", () => {
    expect(splitLinks("자세히: https://kosis.kr/a?b=1 (참고)")).toEqual([
      { text: "자세히: " },
      { text: "https://kosis.kr/a?b=1", href: "https://kosis.kr/a?b=1" },
      { text: " (참고)" },
    ]);
    expect(splitLinks("주소 없음")).toEqual([{ text: "주소 없음" }]);
  });
  it("http(s) 가 아닌 주소는 링크로 쓰지 않는다", () => {
    expect(safeUrl("https://kosis.kr/x")).toBe("https://kosis.kr/x");
    expect(safeUrl("javascript:alert(1)")).toBeUndefined();
  });
});

describe("되묻기 선택", () => {
  it("판은 최신을, 대상·성별은 합계가 있을 때만 미리 고른다", () => {
    expect(defaultOption({ axis: "edition", options: ["2015~2024", "2006~2015"] })).toBe("2015~2024");
    expect(defaultOption({ axis: "sex", options: ["계", "남", "여"] })).toBe("계");
    expect(defaultOption({ axis: "target", options: ["혼인 귀화자", "기타"] })).toBeUndefined();
  });
  it("대상·성별은 selections 로, 판은 문장으로 보낸다", () => {
    expect(memberAnswer({ target: "혼인 귀화자", sex: "여", edition: "2007~2015" })).toEqual({
      text: "혼인 귀화자, 여, 2007~2015년판",
      selections: { target: "혼인 귀화자", sex: "여" },
    });
  });
});
