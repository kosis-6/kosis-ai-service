export const PASSWORD_RULE = "6~20자, 영문·숫자·특수기호를 모두 넣어 주세요.";

export function passwordProblem(value: string): string {
  const ok =
    value.length >= 6 &&
    value.length <= 20 &&
    /[A-Za-z]/.test(value) &&
    /[0-9]/.test(value) &&
    /[^A-Za-z0-9\s]/.test(value);
  return ok ? "" : PASSWORD_RULE;
}
