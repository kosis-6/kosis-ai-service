import { useEffect, useLayoutEffect, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Brand } from "../components/Brand";
import { Icon } from "../components/Icon";
import { Mascot } from "../components/Mascot";
import { usePage } from "../components/usePage";
import { LoginPage } from "./LoginPage";
import { SignupDonePage } from "./SignupDonePage";
import { SignupPage } from "./SignupPage";
import "../styles/account.css";

export type AccountMode = "in" | "up";

export function modeOf(pathname: string): AccountMode {
  return pathname.startsWith("/signup") ? "up" : "in";
}

function Back() {
  return (
    <Link className="back" to="/">
      <Icon name="arrowLeft" />
      통계표 찾기로 돌아가기
    </Link>
  );
}

/** 로그인 · 회원가입을 가운데 카드 한 장에 두고, 초록 덮개가 옆으로 밀리며 두 화면을 바꾼다.
 *  주소(/login · /signup)가 어느 쪽을 보일지 정해서 뒤로 가기 · 바로 들어오기도 그대로 된다.
 *  휴대폰 폭에서는 덮개 없이 보이는 쪽 하나만 그린다 (account.css) */
export function AccountLayout() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const mode = modeOf(pathname);
  const done = pathname === "/signup/done";
  usePage("account", done ? "가입 완료" : mode === "up" ? "회원가입" : "로그인");

  // 덮개 버튼으로 바꾸면 그 버튼이 가려진다: 새로 보이는 쪽 첫 칸으로 초점을 옮긴다 (처음 들어올 때는 옮기지 않는다)
  const shown = useRef(mode);
  useEffect(() => {
    if (shown.current === mode) return;
    shown.current = mode;
    document.querySelector<HTMLElement>(`.side.${mode} input, .side.${mode} .btn`)?.focus({ preventScroll: true });
  }, [mode]);

  // 카드 높이는 보이는 쪽 폼에 맞춘다: 로그인은 짧게, 회원가입으로 밀면 덮개와 함께 늘어난다.
  // 인증번호 칸이 생기는 등 폼 높이가 바뀌어도 따라간다 (휴대폰 폭에서는 CSS가 이 값을 쓰지 않는다)
  const card = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = card.current;
    if (!el) return;
    const fit = () => {
      const view = el.querySelector<HTMLElement>(`.side.${mode} .view`);
      const side = view?.closest<HTMLElement>(".side");
      if (!view || !side) return;
      const pad = parseFloat(getComputedStyle(side).paddingTop) + parseFloat(getComputedStyle(side).paddingBottom);
      el.style.setProperty("--entry-h", `${Math.ceil(view.offsetHeight + pad)}px`);
    };
    fit();
    const watch = new ResizeObserver(fit);
    el.querySelectorAll(".side .view").forEach((v) => watch.observe(v));
    return () => watch.disconnect();
  }, [mode, done]);

  return (
    <div className="entry-page">
      <header className="m-head">
        <Link className="m-back" to="/" aria-label="통계표 찾기로 돌아가기">
          <Icon name="arrowLeft" size={22} />
        </Link>
        <Brand />
      </header>
      {/* 넓은 화면: 카드 위에는 돌아가기만 (서비스 표시는 덮개의 캐릭터가 한다) */}
      <div className="entry-top">
        <Back />
      </div>

      <div ref={card} className={mode === "up" ? "entry is-up" : "entry"}>
        <section className="side in" inert={mode !== "in"} role={mode === "in" ? "main" : undefined}>
          <div className="views">
            <LoginPage />
          </div>
        </section>

        <section className="side up" inert={mode !== "up"} role={mode === "up" ? "main" : undefined}>
          <div className="views">{done ? <SignupDonePage /> : <SignupPage />}</div>
        </section>

        <aside className="cover">
          <div className="cover-track">
            <div className="cover-panel to-in" inert={mode !== "up"}>
              <Mascot place="story" className="cover-art" label="LIKE SNAKE 캐릭터" />
              <p className="cover-title">다시 만나서 반가워요</p>
              <p className="cover-text">
                이미 계정이 있다면 로그인하고
                <br />
                지난 대화를 이어서 물어보세요.
              </p>
              <button type="button" className="btn ghost" onClick={() => navigate("/login")}>
                로그인
              </button>
            </div>
            <div className="cover-panel to-up" inert={mode !== "in"}>
              <Mascot place="story" className="cover-art" label="LIKE SNAKE 캐릭터" />
              <p className="cover-title">
                일상 속 질문을,
                <br />
                믿을 수 있는 통계로
              </p>
              <p className="cover-text">
                처음이신가요? 가입하면 질문과 답이 계정에
                <br />
                저장되어 30일 동안 다시 볼 수 있어요.
              </p>
              <button type="button" className="btn ghost" onClick={() => navigate("/signup")}>
                회원가입
              </button>
            </div>
          </div>
        </aside>
      </div>

      <p className="panel-foot">LIKE SNAKE · KOSIS 인구 분야 통계표 안내</p>
    </div>
  );
}
