// 회원가입: ① 인증번호 받기 → ② 인증번호 확인 → ③ 가입
// (POST /auth/email-code → /auth/email-code/verify → /auth/register)
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { EmailCodeSent } from "../api/types";
import { post } from "../auth/client";
import { PASSWORD_RULE, passwordProblem } from "../auth/rules";
import { Field, type Note } from "../components/Field";
import { usePage } from "../components/usePage";

type Notes = Partial<Record<"nickname" | "email" | "code" | "password" | "confirm", Note>>;
type Step = "idle" | "sent" | "verified"; // 이메일 인증 단계

export function SignupPage() {
  usePage("account", "회원가입");
  const navigate = useNavigate();

  const [nickname, setNickname] = useState("");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [notes, setNotes] = useState<Notes>({});
  const [error, setError] = useState("");
  const [step, setStep] = useState<Step>("idle");
  const [verified, setVerified] = useState(""); // 인증을 마친 이메일
  const [wait, setWait] = useState(0); // 인증번호를 다시 받을 수 있을 때까지(초)
  const [busy, setBusy] = useState<"" | "send" | "verify" | "submit">("");

  const emailRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);

  const emailNow = email.trim().toLowerCase();
  const note = (name: keyof Notes, value?: Note) => setNotes((prev) => ({ ...prev, [name]: value }));

  useEffect(() => {
    if (wait <= 0) return;
    const timer = setTimeout(() => setWait((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [wait]);

  // 브라우저 기본 검사 문구로 "비밀번호가 서로 달라요"를 알린다
  useEffect(() => {
    confirmRef.current?.setCustomValidity(
      confirm === password ? "" : "비밀번호가 서로 달라요. 다시 확인해 주세요.",
    );
  }, [confirm, password]);

  function resetVerify() {
    setStep("idle");
    setVerified("");
    setWait(0);
    setCode("");
    note("code");
  }

  // 이메일을 고치면 인증을 처음부터 다시 한다
  function changeEmail(value: string) {
    setEmail(value);
    if (step !== "idle" || wait) resetVerify();
    note("email");
  }

  async function sendCode() {
    if (!emailRef.current?.checkValidity()) {
      emailRef.current?.reportValidity();
      return;
    }
    setBusy("send");
    const r = await post<EmailCodeSent>("/auth/email-code", { email: emailNow });
    setBusy("");
    if (!r.ok) {
      note("email", { text: r.status === 422 ? "이메일 주소를 다시 확인해 주세요." : r.message, kind: "bad" });
      if (r.status === 429 && r.wait) setWait(r.wait);
      return;
    }
    const minutes = Math.round(r.data.expires_in / 60);
    note("email", { text: `인증번호를 메일로 보냈어요. ${minutes}분 안에 입력해 주세요.`, kind: "ok" });
    setStep("sent");
    setCode("");
    note("code");
    setWait(r.data.resend_after);
    setTimeout(() => codeRef.current?.focus());
  }

  async function verifyCode() {
    const value = code.trim();
    if (!/^\d{6}$/.test(value)) {
      note("code", { text: "메일로 받은 6자리 숫자를 입력해 주세요.", kind: "bad" });
      return;
    }
    setBusy("verify");
    const r = await post("/auth/email-code/verify", { email: emailNow, code: value });
    setBusy("");
    if (!r.ok) {
      note("code", { text: r.fields.code || r.message, kind: "bad" });
      codeRef.current?.select();
      return;
    }
    resetVerify();
    setStep("verified");
    setVerified(emailNow);
    note("email", { text: "이메일 인증을 마쳤어요.", kind: "ok" });
    passwordRef.current?.focus();
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (step !== "verified" || verified !== emailNow) {
      note("email", { text: "이메일 인증을 먼저 해 주세요.", kind: "bad" });
      emailRef.current?.focus();
      return;
    }
    const weak = passwordProblem(password);
    if (weak) {
      note("password", { text: weak, kind: "bad" });
      passwordRef.current?.focus();
      return;
    }
    setBusy("submit");
    const r = await post("/auth/register", { email: verified, password, nickname });
    setBusy("");
    if (r.ok) {
      navigate("/signup/done");
      return;
    }
    for (const name of ["nickname", "email", "password"] as const) {
      if (r.fields[name]) note(name, { text: r.fields[name], kind: "bad" });
    }
    if (r.status === 400 || r.status === 409) {
      // 400: 인증한 지 30분이 지남 → 다시 인증. 409: 그사이 누가 같은 이메일로 가입함
      resetVerify();
      note("email", { text: r.message, kind: "bad" });
    } else if (!Object.keys(r.fields).length) {
      setError(r.message);
    }
  }

  const sendLabel =
    step === "verified" ? "인증 완료" : wait > 0 ? `다시 받기 (${wait}초)` : step === "sent" ? "다시 받기" : "인증번호 받기";

  return (
    <form className="view compact" onSubmit={submit}>
      <div className="form-head">
        <p className="eyebrow">START WITH SIX</p>
        <h1>함께 시작해 볼까요?</h1>
        <p className="lead">간단한 가입으로 통계를 더 가까이 만나 보세요.</p>
      </div>
      <div className="fields">
        <Field
          label="닉네임"
          type="text"
          name="nickname"
          placeholder="사용할 이름을 입력해 주세요"
          autoComplete="nickname"
          minLength={2}
          maxLength={20}
          required
          note={notes.nickname}
          value={nickname}
          onChange={(e) => {
            setNickname(e.target.value);
            note("nickname");
          }}
        />
        <Field
          ref={emailRef}
          label="이메일"
          type="email"
          name="email"
          placeholder="이메일 주소를 입력해 주세요"
          autoComplete="email"
          maxLength={320}
          required
          note={notes.email}
          value={email}
          onChange={(e) => changeEmail(e.target.value)}
          action={
            <button
              type="button"
              className="field-btn"
              disabled={step === "verified" || wait > 0 || busy === "send"}
              aria-busy={busy === "send"}
              onClick={() => void sendCode()}
            >
              {sendLabel}
            </button>
          }
        />
        {step === "sent" && (
          <Field
            ref={codeRef}
            label="인증번호"
            type="text"
            name="code"
            placeholder="메일로 받은 6자리 숫자"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            note={notes.code}
            value={code}
            onChange={(e) => {
              setCode(e.target.value);
              note("code");
            }}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault(); // 가입 폼이 제출되지 않게
              void verifyCode();
            }}
            action={
              <button
                type="button"
                className="field-btn"
                disabled={busy === "verify"}
                aria-busy={busy === "verify"}
                onClick={() => void verifyCode()}
              >
                확인
              </button>
            }
          />
        )}
        <Field
          ref={passwordRef}
          label="비밀번호"
          type="password"
          name="password"
          placeholder="사용할 비밀번호를 입력해 주세요"
          autoComplete="new-password"
          maxLength={20}
          required
          note={notes.password}
          hint={PASSWORD_RULE}
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            note("password");
          }}
        />
        <Field
          ref={confirmRef}
          label="비밀번호 확인"
          type="password"
          name="confirm"
          placeholder="비밀번호를 다시 입력해 주세요"
          autoComplete="new-password"
          required
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </div>
      <div className="agrees">
        <label className="agree">
          <input type="checkbox" name="terms" required /> 서비스 이용약관 동의 (필수)
        </label>
        <label className="agree">
          <input type="checkbox" name="privacy" required /> 개인정보 수집·이용 동의 (필수)
        </label>
      </div>
      {error && (
        <p className="form-msg" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="btn primary block" disabled={busy === "submit"} aria-busy={busy === "submit"}>
        회원가입
      </button>
      <p className="switch">
        {"이미 계정이 있으신가요?  "}
        <Link to="/login">로그인</Link>
      </p>
    </form>
  );
}
