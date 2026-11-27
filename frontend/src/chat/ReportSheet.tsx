import { useMemo, useRef, useState } from "react";
import type { AskResponse } from "../api/types";
import { Icon } from "../components/Icon";
import { pdfName, reportDocument } from "./report";
import { createReport, MAX_ITEMS, pickable, type Report } from "./reports";
import { Sheet } from "./Sheet";

type Props = { sessionId: string | null; answers: AskResponse[]; onClose: () => void };

/** 만든 보고서를 앱 안에서 먼저 보여 준다. [PDF로 저장]은 이 문서만 인쇄 창으로 보낸다 (대화 화면은 찍히지 않는다) */
export function ReportPreview({ report, onBack }: { report: Report; onBack: () => void }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [at] = useState(() => new Date());
  const doc = useMemo(() => reportDocument(report, at), [report, at]);

  function save() {
    const win = frame.current?.contentWindow;
    if (!win) return;
    // 'PDF로 저장' 파일 이름은 문서 제목: 브라우저에 따라 바깥 페이지 제목을 쓰기도 해서 인쇄하는 동안 맞춘다
    const before = document.title;
    document.title = pdfName(report, at);
    try {
      win.focus();
      win.print();
    } finally {
      document.title = before;
    }
  }

  return (
    <>
      <div className="report-view-bar">
        <button type="button" className="text-btn" onClick={onBack}>
          <Icon name="arrowLeft" size={14} />
          답 다시 고르기
        </button>
        <p className="sheet-text">인쇄 창의 대상에서 ‘PDF로 저장’을 고르면 PDF 파일로 받아요.</p>
        <button type="button" className="btn primary" onClick={save}>
          PDF로 저장
        </button>
      </div>
      {/* 스크립트는 돌지 않게 막고, 인쇄 창과 출처 링크(새 탭)만 허용한다 */}
      <iframe
        ref={frame}
        className="report-frame"
        title="보고서 미리보기"
        srcDoc={doc}
        sandbox="allow-same-origin allow-modals allow-popups allow-popups-to-escape-sandbox"
      />
    </>
  );
}

/** 대화창 [보고서]: 이 대화의 답 중 고른 것만으로 보고서를 만들고, 앱 안에서 먼저 보여 준다
 *  (한 대화의 답만, 1개부터 MAX_ITEMS개까지) */
export function ReportSheet({ sessionId, answers, onClose }: Props) {
  const picks = pickable(answers);
  const [chosen, setChosen] = useState<number[]>([]);
  const [making, setMaking] = useState(false);
  const [made, setMade] = useState<Report | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const full = chosen.length >= MAX_ITEMS;

  function toggle(turnId: number) {
    setMessage(null);
    setChosen((prev) => (prev.includes(turnId) ? prev.filter((t) => t !== turnId) : [...prev, turnId]));
  }

  async function make() {
    if (!chosen.length || making) return;
    setMaking(true);
    setMessage(null);
    // 보고서에는 대화 순서대로 담는다
    const order = picks.map((p) => p.turnId).filter((t) => chosen.includes(t));
    try {
      const report = await createReport(sessionId, order);
      if (report.status !== "ready") {
        setMessage("보고서 문장을 만들지 못했어요. 잠시 후 다시 시도해 주세요.");
        return;
      }
      setMade(report);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "보고서를 만들지 못했어요. 다시 시도해 주세요.");
    } finally {
      setMaking(false);
    }
  }

  if (made) {
    return (
      <Sheet title="보고서 미리보기" doc onClose={onClose}>
        <ReportPreview report={made} onBack={() => setMade(null)} />
      </Sheet>
    );
  }

  if (!picks.length) {
    return (
      <Sheet title="보고서 만들기" onClose={onClose}>
        <p className="sheet-text">
          이 대화에는 보고서에 담을 답이 아직 없어요. 수치를 계산해 답한 결과가 있으면 그 답으로 보고서를 만들 수 있어요.
        </p>
      </Sheet>
    );
  }

  return (
    <Sheet title="보고서 만들기" onClose={onClose}>
      <p className="sheet-text">
        이 대화에서 수치로 답한 결과 중 보고서에 담을 것을 고르세요. 1개부터 {MAX_ITEMS}개까지 고를 수 있어요.
        요약은 보고서가 고른 답으로 직접 만들어요.
      </p>
      <ul className="report-picks">
        {picks.map((p) => {
          const picked = chosen.includes(p.turnId);
          return (
            <li key={p.turnId}>
              <label className="report-pick">
                <input
                  type="checkbox"
                  checked={picked}
                  disabled={making || (full && !picked)}
                  onChange={() => toggle(p.turnId)}
                />
                <span className="report-pick-text">
                  <span className="report-pick-q">{p.question || "질문"}</span>
                  <span className="report-pick-a">{p.answer}</span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>
      <div className="report-actions">
        <span className="sheet-text">{`${chosen.length}/${MAX_ITEMS} 선택`}</span>
        <button type="button" className="btn primary" disabled={!chosen.length || making} onClick={() => void make()}>
          {making ? "보고서를 만들고 있어요" : "보고서 만들기"}
        </button>
      </div>
      {message && (
        <p className="sheet-text" role="alert">
          {message}
        </p>
      )}
    </Sheet>
  );
}
