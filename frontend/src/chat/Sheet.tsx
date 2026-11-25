import { useEffect, useRef, type ReactNode } from "react";
import { Icon } from "../components/Icon";

type Props = { title: string; narrow?: boolean; onClose: () => void; children: ReactNode };

export function Sheet({ title, narrow, onClose, children }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className={narrow ? "sheet narrow" : "sheet"}
      aria-labelledby="sheet-title"
      onClose={onClose}
      onClick={(e) => {
        if (e.target === ref.current) ref.current.close();
      }}
    >
      <div className="sheet-head">
        <h2 id="sheet-title">{title}</h2>
        <button type="button" className="text-btn" onClick={() => ref.current?.close()}>
          닫기
          <Icon name="x" size={14} />
        </button>
      </div>
      <div className="sheet-body">{children}</div>
    </dialog>
  );
}
