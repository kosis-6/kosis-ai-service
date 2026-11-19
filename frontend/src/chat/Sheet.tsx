// 추가 상태 패널 (다른 후보, 다른 질문 예시, 지난 대화). <dialog> 로 띄운다
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
      // 패널 바깥(배경)을 누르면 닫는다
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
