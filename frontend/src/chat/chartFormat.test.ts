import { describe, expect, it } from "vitest";
import { splitAnswer } from "./Answer";
import { PART_COLORS, axisTick, fitsLabel, formatValue, labelWidth, rowLabel, shortUnit, withUnit } from "./chartFormat";

describe("차트 숫자", () => {
  it("값은 자릿수 쉼표로, 없으면 줄표", () => {
    expect(formatValue(9331828)).toBe("9,331,828");
    expect(formatValue(0.581)).toBe("0.581");
    expect(formatValue(null)).toBe("–");
  });
  it("짧은 단위만 값 옆에 붙인다", () => {
    expect(withUnit(23.9, "%")).toBe("23.9%");
    expect(withUnit(238317, "명")).toBe("238,317명");
    expect(withUnit(0.581, "가임여성 1명당 명")).toBe("0.581");
    expect(shortUnit(null)).toBe("");
  });
  it("축 눈금은 만 이상이 있는 축에서만 줄여 쓴다", () => {
    expect(axisTick(20000, 60000)).toBe("2만");
    expect(axisTick(500, 900)).toBe("500");
    expect(axisTick(0.5, 1.2)).toBe("0.5");
  });
  it("연도 행은 '년'을 붙인다", () => {
    expect(rowLabel({ category: "연도" }, { label: "2024" })).toBe("2024년");
    expect(rowLabel({ category: "지역" }, { label: "서울특별시" })).toBe("서울특별시");
  });
  it("이름 칸은 가장 긴 이름에 맞추되 넓이를 제한한다", () => {
    expect(labelWidth([{ label: "서울특별시" }])).toBe(73);
    expect(labelWidth([{ label: "가".repeat(40) }])).toBe(168);
    expect(labelWidth([])).toBe(48);
  });
  it("구성비 색은 서버가 보내는 조각 수(6개)만큼 있고, 좁은 조각에는 글자를 넣지 않는다", () => {
    expect(PART_COLORS).toHaveLength(6);
    expect(fitsLabel(8.96)).toBe(false);
    expect(fitsLabel(19.44)).toBe(true);
  });
});

describe("답변 나누기", () => {
  it("첫 문단은 말풍선, 나머지에서 출처 줄은 뺀다", () => {
    const answer = [
      "2024년 부산광역시의 고령인구 비율은 23.9%입니다.",
      "",
      "※ 계산식: 65세 이상 인구 ÷ 총인구 × 100",
      "출처: 국가데이터처, 주민등록인구 (DT_1B04006)",
      "통계표: https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_1B04006",
    ].join("\n");
    expect(splitAnswer(answer)).toEqual({
      lead: "2024년 부산광역시의 고령인구 비율은 23.9%입니다.",
      detail: "※ 계산식: 65세 이상 인구 ÷ 총인구 × 100",
    });
  });
  it("한 문단뿐이면 나머지는 없다", () => {
    expect(splitAnswer("지역별 인구통계 질문에만 답할 수 있습니다.")).toEqual({
      lead: "지역별 인구통계 질문에만 답할 수 있습니다.",
      detail: "",
    });
  });
});
