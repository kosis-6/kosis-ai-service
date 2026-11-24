import { lazy, Suspense, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { ready } from "../auth/client";
import { useUser } from "../auth/useUser";

const AdminPage = lazy(() => import("../pages/AdminPage").then((m) => ({ default: m.AdminPage })));

export function AdminGate({ view }: { view: "sessions" | "searches" }) {
  const user = useUser();
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
