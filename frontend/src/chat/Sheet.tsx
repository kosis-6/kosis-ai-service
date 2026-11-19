// 추가 상태 패널 (다른 후보, 다른 질문 예시, 개발 도구). <dialog> 로 띄운다
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

type DevProps = {
  mock: boolean;
  onMock: () => void;
};

/** 개발 도구: 목업 전환 (?mock=1 로 켰을 때만 보인다). 처리 과정·후보 점수는 관리자 화면의 사용 기록에서 본다 */
export function DevTools({ mock, onMock }: DevProps) {
  return (
    <>
      <div className="dev-options">
        <button type="button" className="dev-toggle" aria-pressed={mock} onClick={onMock}>
          {`목업 모드  ${mock ? "● 켜짐" : "○ 꺼짐"}`}
        </button>
      </div>
      {mock && <p className="dev-note">화면 개발용 목업 결과입니다. 통계 수치를 생성하지 않습니다.</p>}
    </>
  );
}
