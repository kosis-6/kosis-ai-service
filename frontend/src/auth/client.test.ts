import { describe, expect, it } from "vitest";
import { toFailure } from "./client";
import { passwordProblem } from "./rules";

describe("서버 오류 읽기", () => {
  it("직접 낸 오류(글자)는 문구로", () => {
    const f = toFailure(429, { detail: "인증번호는 42초 뒤에 다시 받을 수 있습니다." }, "42");
    expect(f).toMatchObject({ status: 429, message: "인증번호는 42초 뒤에 다시 받을 수 있습니다.", wait: 42 });
    expect(f.fields).toEqual({});
  });
  it("입력값 검사(422)는 칸마다", () => {
    const f = toFailure(422, {
      detail: [
        { loc: ["body", "nickname"], msg: "닉네임은 2~20자로 적어 주세요." },
        { loc: ["body", "password"], msg: "비밀번호에 숫자를 넣어 주세요." },
      ],
    });
    expect(f.fields).toEqual({
      nickname: "닉네임은 2~20자로 적어 주세요.",
      password: "비밀번호에 숫자를 넣어 주세요.",
    });
  });
  it("본문이 없으면 기본 문구", () => {
    expect(toFailure(500, null).message).toContain("서버에 연결하지 못했어요");
    expect(toFailure(400, null).message).toBe("요청을 처리하지 못했어요.");
  });
});

describe("비밀번호 규칙 (서버와 같게)", () => {
  it.each(["pw1234!", "Abc12#", "a1!a1!a1!a1!a1!a1!a1"])("맞음: %s", (v) => {
    expect(passwordProblem(v)).toBe("");
  });
  it.each(["a1!", "abcdef1", "abcdef!", "123456!", "pw 123456", "a1!".repeat(7)])("틀림: %s", (v) => {
    expect(passwordProblem(v)).not.toBe("");
  });
});
