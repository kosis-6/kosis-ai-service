import { describe, expect, it } from "vitest";
import { splitAnswer } from "./Answer";
import {
  LINE_COLORS,
  MAX_OVERLAP,
  PART_COLORS,
  axisTick,
  changeText,
  fitsLabel,
  formatValue,
  labelWidth,
  panels,
  rowLabel,
  seriesMax,
  seriesRows,
  shortUnit,
  withUnit,
} from "./chartFormat";

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

describe("여러 꺾은선", () => {
  const chart = {
    series: ["서울특별시", "부산광역시"],
    rows: [
      { label: "2022", value: null, values: [0.59, null] },
      { label: "2023", value: null, values: [0.55, -0.66] },
    ],
  };
  it("행을 연도와 지역별 칸(s0, s1)으로 바꾸고, 없는 값은 비운다", () => {
    expect(seriesRows(chart)).toEqual([
      { year: 2022, s0: 0.59, s1: null },
      { year: 2023, s0: 0.55, s1: -0.66 },
    ]);
  });
  it("축 눈금은 모든 지역 값 중 가장 큰 절댓값으로 정한다", () => {
    expect(seriesMax(chart)).toBe(0.66);
  });
  it("선 색은 서버가 보내는 선 수(6개)만큼 있다", () => {
    expect(LINE_COLORS).toHaveLength(6);
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

describe("지역마다 작은 그래프", () => {
  const chart = {
    series: ["서울", "세종"],
    rows: [
      { label: "2018", value: null, values: [100, null] },
      { label: "2019", value: null, values: [90, 30] },
      { label: "2020", value: null, values: [95.5, 33] },
    ],
  };

  it("지역마다 연도별 점과 처음 대비 마지막 증감률을 만든다", () => {
    const [seoul, sejong] = panels(chart);
    expect(seoul.points.map((p) => p.value)).toEqual([100, 90, 95.5]);
    expect(seoul.change).toBe(-4.5);
    expect(sejong.change).toBe(10); // 값이 있는 처음 해(2019)부터
    expect(changeText(seoul.change)).toBe("-4.5%");
    expect(changeText(sejong.change)).toBe("+10%");
    expect(changeText(null)).toBe("");
  });

  it("겹쳐 그리는 선은 색 개수까지", () => {
    expect(MAX_OVERLAP).toBe(LINE_COLORS.length);
  });
});
