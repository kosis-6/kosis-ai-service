import { Link, Outlet } from "react-router-dom";
import { Brand } from "../components/Brand";
import { Icon } from "../components/Icon";
import "../styles/account.css";

export function AccountLayout() {
  return (
    <>
      <aside className="story">
        <Brand className="on-dark" />
        <div className="story-body">
          <span className="story-art">
            <span className="logo" role="img" aria-label="LIKE SNAKE 로고" />
          </span>
          <p className="story-title">
            일상 속 질문을,
            <br />
            믿을 수 있는 통계로
          </p>
          <p className="story-text">
            복잡한 통계표 찾기는 LIKE SNAKE에게 맡기세요.
            <br />
            궁금한 순간부터 필요한 자료에 닿을 때까지.
          </p>
          <ul className="story-topics">
            <li>인구</li>
            <li>가구</li>
            <li>이동</li>
          </ul>
        </div>
        <p className="story-foot">LIKE SNAKE · 통계와 일상을 잇다</p>
      </aside>

      <div className="panel">
        <header className="m-head">
          <Link className="m-back" to="/" aria-label="통계표 찾기로 돌아가기">
            <Icon name="arrowLeft" size={22} />
          </Link>
          <Brand />
        </header>
        <Link className="back" to="/">
          <Icon name="arrowLeft" />
          통계표 찾기로 돌아가기
        </Link>
        <main className="views">
          <Outlet />
        </main>
        <p className="panel-foot">LIKE SNAKE · KOSIS 인구 분야 통계표 안내</p>
      </div>
    </>
  );
}
