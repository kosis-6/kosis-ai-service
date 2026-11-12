// SIX / Input: 이름표 + 입력 칸 + 칸 아래 안내·오류 문구
import { useState, type InputHTMLAttributes, type ReactNode, type Ref } from "react";
import iconEye from "../assets/icon-eye.svg";

export type Note = { text: string; kind?: "bad" | "ok" };

type Props = InputHTMLAttributes<HTMLInputElement> & {
  ref?: Ref<HTMLInputElement>;
  label: string;
  note?: Note | null; // 없으면 hint 를 보여 준다
  hint?: string;
  action?: ReactNode; // 칸 안 오른쪽 버튼 (인증번호 받기·확인)
};

export function Field({ ref, label, note, hint, action, type, ...input }: Props) {
  const [showing, setShowing] = useState(false); // 비밀번호 보기
  const password = type === "password";
  const text = note?.text || hint;
  return (
    <label className={note?.kind === "bad" ? "field invalid" : "field"}>
      <span className="field-label">{label}</span>
      <span className="field-box">
        <input ref={ref} type={password && showing ? "text" : type} {...input} />
        {password && (
          <button
            type="button"
            className="eye"
            aria-label={showing ? "비밀번호 숨기기" : "비밀번호 보기"}
            aria-pressed={showing}
            onClick={() => setShowing((on) => !on)}
          >
            <img src={iconEye} width="18" height="18" alt="" />
          </button>
        )}
        {action}
      </span>
      {text && (
        <span className={`field-msg ${note?.text ? (note.kind ?? "") : ""}`.trim()} aria-live="polite">
          {text}
        </span>
      )}
    </label>
  );
}
