// 서버(backend/app/schemas/auth.py)와 같은 입력 규칙. 보내기 전에 먼저 알려 주려는 것이고,
// 진짜 검사는 서버가 한다
export const PASSWORD_RULE = "6~20자, 영문·숫자·특수기호를 모두 넣어 주세요.";

/** 규칙에 맞으면 "", 아니면 고칠 점 */
export function passwordProblem(value: string): string {
  const ok =
    value.length >= 6 &&
    value.length <= 20 &&
    /[A-Za-z]/.test(value) &&
    /[0-9]/.test(value) &&
    /[^A-Za-z0-9\s]/.test(value);
  return ok ? "" : PASSWORD_RULE;
}
