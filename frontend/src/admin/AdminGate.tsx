// 관리자 화면의 문: 관리자로 로그인한 사람에게만 화면을 그린다
// 손님·일반 사용자에게는 관리자 화면이 있다는 것도 보이지 않게, 없는 주소처럼 통계 찾기로 돌려보낸다.
// 화면 코드도 관리자일 때만 내려받는다. 기록 자체는 서버가 요청마다 관리자인지 다시 확인한다
import { lazy, Suspense, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { ready } from "../auth/client";
import { useUser } from "../auth/useUser";

const AdminPage = lazy(() => import("../pages/AdminPage").then((m) => ({ default: m.AdminPage })));

export function AdminGate({ view }: { view: "sessions" | "searches" }) {
  const user = useUser();
  // 브라우저에 남은 표시(이름·관리자 여부)는 믿지 않고, 서버에 로그인을 확인한 뒤에 정한다
  const [checked, setChecked] = useState(false);
  useEffect(() => {
    let live = true;
    void ready.then(() => live && setChecked(true));
    return () => {
      live = false;
    };
  }, []);

  if (!checked) return null;
  if (!user?.admin) return <Navigate to="/" replace />;
  return (
    <Suspense fallback={null}>
      <AdminPage view={view} />
    </Suspense>
  );
}
