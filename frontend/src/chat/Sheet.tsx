// 추가 상태 패널 (다른 후보, 다른 질문 예시, 개발 도구). <dialog> 로 띄운다
import { useEffect, useRef, type ReactNode } from "react";
import type { SearchResponse } from "../api/types";

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
          {"닫기  ×"}
        </button>
      </div>
      <div className="sheet-body">{children}</div>
    </dialog>
  );
}

type DevProps = {
  data: SearchResponse | null; // 디버그 정보를 볼 답변 (머리글 버튼으로 열면 없음)
  mock: boolean;
  debug: boolean;
  onMock: () => void;
  onDebug: () => void;
};

/** 개발 도구: 목업·디버그 전환, 검색 과정 (?mock=1 / ?debug=1 로 켰을 때만 보인다) */
export function DevTools({ data, mock, debug, onMock, onDebug }: DevProps) {
  const d = data?.debug;
  const cands = d?.candidates ?? [];
  const toggle = (label: string, on: boolean, change: () => void) => (
    <button type="button" className="dev-toggle" aria-pressed={on} onClick={change}>
      {`${label}  ${on ? "● 켜짐" : "○ 꺼짐"}`}
    </button>
  );
  return (
    <>
      <div className="dev-options">
        {toggle("목업 모드", mock, onMock)}
        {toggle("디버그", debug, onDebug)}
      </div>
      {mock && <p className="dev-note">화면 개발용 목업 결과입니다. 통계 수치를 생성하지 않습니다.</p>}
      {data && d && (
        <>
          <div className="dev-summary">
            {[
              `status: ${data.status}`,
              `retrieval: ${d.retrieval || "-"}`,
              `query_type: ${data.parsed?.query_type || "-"}`,
              `topic: ${data.parsed?.topic || "-"}`,
              `후보 수: ${cands.length}`,
            ].join("\n")}
          </div>
          {cands.length > 0 && <p className="dev-heading">후보 통계표</p>}
          {cands.map((c) => (
            <p className="dev-cand" key={c.tbl_id}>
              {`${c.tbl_id} · ${c.table_name}${c.score == null ? "" : ` · ${c.score.toFixed(3)}`}`}
            </p>
          ))}
          <details className="dev-raw">
            <summary>원본 값 보기</summary>
            <pre>{JSON.stringify({ session_id: data.session_id, parsed: data.parsed, debug: d }, null, 2)}</pre>
          </details>
        </>
      )}
    </>
  );
}
