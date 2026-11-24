import { Link, NavLink } from "react-router-dom";
import { SearchesView } from "../admin/SearchesView";
import { SessionsView } from "../admin/SessionsView";
import { logout } from "../auth/client";
import { useUser } from "../auth/useUser";
import { Brand } from "../components/Brand";
import { Icon } from "../components/Icon";
import { usePage } from "../components/usePage";
import "../styles/chat.css";
import "../styles/admin.css";

const VIEWS = {
  sessions: { title: "대화 기록", to: "/admin", icon: "chart" },
  searches: { title: "통계표 찾기 기록", to: "/admin/searches", icon: "search" },
} as const;

export function AdminPage({ view }: { view: keyof typeof VIEWS }) {
  usePage("admin", VIEWS[view].title);
  const user = useUser();

  const links = Object.values(VIEWS).map((v) => (
    <NavLink key={v.to} className="menu-item" to={v.to} end>
      <Icon name={v.icon} size={18} />
      {v.title}
    </NavLink>
  ));

  return (
    <>
      <aside className="sidebar">
        <div className="side-top">
          <Brand />
          <p className="side-label">관리자</p>
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
                {Array.from(user.name)[0]}
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
