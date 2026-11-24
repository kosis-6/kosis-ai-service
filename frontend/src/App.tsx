import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AdminGate } from "./admin/AdminGate";
import { AccountLayout } from "./pages/AccountLayout";
import { ChatPage } from "./pages/ChatPage";
import { LoginPage } from "./pages/LoginPage";
import { SignupDonePage } from "./pages/SignupDonePage";
import { SignupPage } from "./pages/SignupPage";

export function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<ChatPage />} />
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
