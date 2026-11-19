import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AdminGate } from "./admin/AdminGate";
import { AccountLayout } from "./pages/AccountLayout";
import { ChatPage } from "./pages/ChatPage";
import { LoginPage } from "./pages/LoginPage";
import { SignupDonePage } from "./pages/SignupDonePage";
import { SignupPage } from "./pages/SignupPage";

// 주소는 /chat 아래에 둔다 (vite.config.ts 의 base). FastAPI 가 없는 경로에도 index.html 을 준다
export function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        {/* 로그인 유저의 대화 하나. 새로고침하면 서버 기록으로 다시 그린다 (같은 ChatPage 를 그대로 쓴다) */}
        <Route path="/c/:sessionId" element={<ChatPage />} />
        <Route element={<AccountLayout />}>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/signup/done" element={<SignupDonePage />} />
        </Route>
        <Route path="/admin" element={<AdminGate view="sessions" />} />
        <Route path="/admin/searches" element={<AdminGate view="searches" />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
