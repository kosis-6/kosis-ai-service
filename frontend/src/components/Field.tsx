import { useState, type InputHTMLAttributes, type ReactNode, type Ref } from "react";
import { Icon } from "./Icon";

export type Note = { text: string; kind?: "bad" | "ok" };

type Props = InputHTMLAttributes<HTMLInputElement> & {
  ref?: Ref<HTMLInputElement>;
  label: string;
  note?: Note | null;
  hint?: string;
  action?: ReactNode;
};

export function Field({ ref, label, note, hint, action, type, ...input }: Props) {
  const [showing, setShowing] = useState(false);
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
            <Icon name="eye" size={18} />
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
