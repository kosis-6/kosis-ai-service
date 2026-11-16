// 관리자 화면: 사용 기록 (대화 · 통계표 찾기). 통계 찾기 화면과 같은 틀(사이드바 + 본문)을 쓴다
// 주소는 /chat/admin, /chat/admin/searches. 관리자가 아니면 서버가 403 을 주고, 화면은 그 안내를 보여 준다
import { Link, NavLink } from "react-router-dom";
import { SearchesView } from "../admin/SearchesView";
import { SessionsView } from "../admin/SessionsView";
import iconChart from "../assets/icon-chart.svg";
import iconSearch from "../assets/icon-search.svg";
import { logout } from "../auth/client";
import { useUser } from "../auth/useUser";
import { Brand } from "../components/Brand";
import { usePage } from "../components/usePage";
import "../styles/chat.css";
import "../styles/admin.css";

const VIEWS = {
  sessions: { title: "대화 기록", to: "/admin", icon: iconChart },
  searches: { title: "통계표 찾기 기록", to: "/admin/searches", icon: iconSearch },
} as const;

export function AdminPage({ view }: { view: keyof typeof VIEWS }) {
  usePage("admin", VIEWS[view].title);
  const user = useUser();

  const links = Object.values(VIEWS).map((v) => (
    <NavLink key={v.to} className="menu-item" to={v.to} end>
      <img src={v.icon} width="18" height="18" alt="" />
      {v.title}
    </NavLink>
  ));

  return (
    <>
      <aside className="sidebar">
        <div className="side-top">
          <Brand />
          <p className="side-label">ADMIN</p>
          <nav className="menu" aria-label="관리자 메뉴">
            {links}
          </nav>
        </div>
        <div className="side-bottom">
          <Link className="menu-item" to="/">
            통계 찾기로 돌아가기
          </Link>
          {user && (
            <div className="account">
              <span className="account-mark" aria-hidden="true">
                S
              </span>
              <span className="account-name">{user.name}</span>
              <button type="button" className="text-btn" onClick={() => void logout()}>
                로그아웃
              </button>
            </div>
          )}
        </div>
      </aside>

      <div className="main admin">
        <header className="head">
          <div className="service only-wide">
            <p className="service-tag">관리자</p>
            <h1 className="service-name">{VIEWS[view].title}</h1>
          </div>
          <Brand className="only-narrow" />
          <div className="head-actions">
            <Link className="btn quiet head-btn only-narrow" to="/">
              통계 찾기
            </Link>
            {user && (
              <p className="head-name only-wide">
                <span>{user.name}</span>님
              </p>
            )}
          </div>
        </header>
        <nav className="admin-tabs only-narrow" aria-label="관리자 메뉴">
          {links}
        </nav>
        <main className="admin-body">{view === "sessions" ? <SessionsView /> : <SearchesView />}</main>
      </div>
    </>
  );
}
