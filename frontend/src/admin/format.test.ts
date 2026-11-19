import { describe, expect, it } from "vitest";
import { sinceDays } from "./api";
import {
  changes,
  conditionLine,
  day,
  filled,
  flowLabel,
  person,
  requestLabel,
  seconds,
  statusLabel,
  stuckStep,
  when,
} from "./format";

describe("시각·숫자", () => {
  it("한국 시간으로 보여 준다", () => {
    expect(when("2026-11-15T08:37:04+00:00")).toBe("11.15 17:37");
    expect(when("2026-11-15T15:30:00Z")).toBe("11.16 00:30"); // 날짜가 넘어간다
    expect(day("2026-11-05")).toBe("11.05");
  });
  it("걸린 시간은 초로, 기록이 없으면 비운다", () => {
    expect(seconds(4200)).toBe("4.2초");
    expect(seconds(null)).toBe("");
  });
  it("기간의 시작 시각", () => {
    expect(sinceDays(7, new Date("2026-11-16T00:00:00Z"))).toBe("2026-11-09T00:00:00.000Z");
  });
});

describe("이름 붙이기", () => {
  it("아는 status 는 우리말로, 모르는 값은 코드 그대로", () => {
    expect(statusLabel("ok")).toEqual({ text: "답함", tone: "good" });
    expect(statusLabel("out_of_scope").tone).toBe("bad");
    expect(statusLabel("brand_new")).toEqual({ text: "brand_new", tone: "plain" });
    expect(statusLabel(null).text).toBe("기록 없음");
    // 통계표 찾기의 ok 는 표를 찾았다는 뜻이다
    expect(statusLabel("ok", true).text).toBe("찾음");
    expect(statusLabel("ambiguous", true).text).toBe("되묻기");
    expect(requestLabel("change")).toBe("조건 변경");
    expect(requestLabel("later")).toBe("later");
  });
  it("누가 남겼는지: 닉네임 → 이메일 → 비로그인 사용자", () => {
    expect(person({ user_id: 3, user_email: "a@b.co", user_nickname: "통계왕" })).toBe("통계왕");
    expect(person({ user_id: 3, user_email: "a@b.co" })).toBe("a@b.co");
    expect(person({ user_id: null })).toBe("비로그인 사용자");
    expect(person({ user_id: 9 })).toBe("사용자 9"); // 계정은 있는데 정보가 안 붙은 경우
  });
});

describe("조건", () => {
  const before = { indicator: "총인구", regions: ["부산"], start_year: 2021, end_year: 2025, sex: "계", type: "trend", term: "총인구" };

  it("바뀐 조건만 고른다", () => {
    const after = { ...before, regions: ["부산", "서울"], term: "다른 말" };
    expect(changes(before, after)).toEqual([{ name: "지역", from: "부산", to: "부산, 서울" }]);
  });
  it("값이 생기거나 없어진 것, 순서·종류는 풀어서", () => {
    const after = { ...before, type: "rank", ascending: true, sex: null };
    expect(changes({ ...before, ascending: false }, after)).toEqual([
      { name: "질문 종류", from: "추이", to: "순위" },
      { name: "성별", from: "계", to: "없음" },
      { name: "순서", from: "높은 순", to: "낮은 순" },
    ]);
  });
  it("첫 질문(앞 조건 없음)은 바뀐 것이 없다", () => {
    expect(changes(null, before)).toEqual([]);
    expect(changes(before, before)).toEqual([]);
  });
  it("조건 한 줄", () => {
    expect(conditionLine(before)).toBe("총인구 · 부산 · 2021~2025 · 추이");
    expect(conditionLine({ indicator: "출생아 수", regions: [], year: 2024, end_year: 2024, sex: "여", type: "value" })).toBe(
      "출생아 수 · 2024 · 여 · 값",
    );
    expect(conditionLine(null)).toBe("");
  });
  it("통계표 찾기 조건은 값이 있는 칸만", () => {
    expect(filled({ sex: null, region: "부산", classes: [], items: ["30세"], year: 2024 })).toEqual([
      ["region", "부산"],
      ["items", "30세"],
      ["year", "2024"],
    ]);
    expect(filled(null)).toEqual([]);
  });
});

describe("처리 흐름", () => {
  const step = (stage: string, result: string) => ({ stage, result, detail: "" });
  it("결과를 말로, 실패는 붉게", () => {
    expect(flowLabel("ok")).toEqual({ text: "성공", tone: "good" });
    expect(flowLabel("tables").text).toBe(statusLabel("tables").text); // /ask status 는 같은 이름
    expect(flowLabel("못 함").tone).toBe("bad");
    expect(flowLabel("HCX").tone).toBe("plain");
  });
  it("최종 답이 실패면 마지막으로 막힌 단계", () => {
    const flow = [
      step("질문 이해", "HCX"),
      step("사전 계산", "unknown_indicator"),
      step("통계표 찾기", "ok"),
      step("표 칸으로 계산", "못 함"),
      step("최종 답", "tables"),
    ];
    expect(stuckStep(flow)?.stage).toBe("표 칸으로 계산");
    expect(stuckStep([step("사전 계산", "ok"), step("최종 답", "ok")])).toBeNull();
    expect(stuckStep(null)).toBeNull();
  });
});
