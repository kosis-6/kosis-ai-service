import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AdminGate } from "./admin/AdminGate";
import { AccountLayout } from "./pages/AccountLayout";
import { ChatPage } from "./pages/ChatPage";

export function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="/c/:sessionId" element={<ChatPage />} />
        {/* 로그인 · 회원가입은 한 장에서 덮개가 밀리며 바뀐다: 두 화면을 AccountLayout이 함께 그린다 */}
        <Route element={<AccountLayout />}>
          <Route path="/login" />
          <Route path="/signup" />
          <Route path="/signup/done" />
        </Route>
        <Route path="/admin" element={<AdminGate view="sessions" />} />
        <Route path="/admin/searches" element={<AdminGate view="searches" />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
