import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { AccountLayout, modeOf } from "./AccountLayout";

const at = (path: string) =>
  renderToStaticMarkup(
    <MemoryRouter initialEntries={[path]}>
      <AccountLayout />
    </MemoryRouter>,
  );

describe("로그인 · 회원가입 한 장", () => {
  it("주소가 어느 쪽을 보일지 정한다", () => {
    expect(modeOf("/login")).toBe("in");
    expect(modeOf("/signup")).toBe("up");
    expect(modeOf("/signup/done")).toBe("up");
  });

  it("로그인: 로그인 폼만 쓸 수 있고, 덮개는 회원가입을 권한다", () => {
    const html = at("/login");
    expect(html).toContain('<div class="entry">');
    expect(html).toContain('<section class="side in" role="main">');
    expect(html).toContain('<section class="side up" inert="">');
    expect(html).toContain('<div class="cover-panel to-up">'); // 보이는 덮개 장
    expect(html).toContain('<div class="cover-panel to-in" inert="">');
  });

  it("회원가입: 덮개가 밀려 회원가입 폼을 쓰고, 덮개는 로그인을 권한다", () => {
    const html = at("/signup");
    expect(html).toContain('<div class="entry is-up">');
    expect(html).toContain('<section class="side in" inert="">');
    expect(html).toContain('<section class="side up" role="main">');
    expect(html).toContain('<div class="cover-panel to-in">');
    expect(html).toContain("다시 만나서 반가워요");
  });

  it("가입 완료는 회원가입 쪽 자리에 보인다", () => {
    const html = at("/signup/done");
    expect(html).toContain("가입이 완료되었어요");
    expect(html).not.toContain("비밀번호 확인");
  });

  it("없는 기능(소셜 로그인 · 비밀번호 찾기)은 두지 않는다", () => {
    const html = at("/login");
    expect(html).not.toMatch(/facebook|google|비밀번호 찾기|비밀번호를 잊/i);
  });
});
