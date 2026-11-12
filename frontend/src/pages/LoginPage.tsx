import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { login } from "../auth/client";
import { Field } from "../components/Field";
import { usePage } from "../components/usePage";

export function LoginPage() {
  usePage("account", "로그인");
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [keep, setKeep] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    const r = await login(email.trim(), password, keep);
    setBusy(false);
    if (!r.ok) {
      setError(r.message);
      return;
    }
    navigate("/");
  }

  return (
    <form className="view" onSubmit={submit}>
      <div className="form-head">
        <p className="eyebrow">WELCOME BACK</p>
        <h1>다시 만나 반가워요</h1>
        <p className="lead">로그인하고 TEAM SIX와 통계 탐색을 시작해 보세요.</p>
      </div>
      <div className="fields">
        <Field
          label="이메일"
          type="email"
          name="email"
          placeholder="이메일 주소를 입력해 주세요"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <Field
          label="비밀번호"
          type="password"
          name="password"
          placeholder="비밀번호를 입력해 주세요"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      <label className="agree">
        <input type="checkbox" name="keep" checked={keep} onChange={(e) => setKeep(e.target.checked)} /> 로그인 상태
        유지
      </label>
      {error && (
        <p className="form-msg" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="btn primary block" disabled={busy} aria-busy={busy}>
        로그인
      </button>
      <p className="switch">
        {"아직 계정이 없으신가요?  "}
        <Link to="/signup">회원가입</Link>
      </p>
    </form>
  );
}
