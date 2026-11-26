import { useState } from "react";
import type { AskResponse } from "../api/types";
import { openBundleReport } from "./report";
import { createReport, MAX_ITEMS, pickable } from "./reports";
import { Sheet } from "./Sheet";

type Props = { sessionId: string | null; answers: AskResponse[]; onClose: () => void };

const OPENED = "보고서를 새 탭으로 열었어요. 같은 대화에서 다른 답을 골라 다시 만들 수 있어요.";
const SAVED = "브라우저가 새 탭을 막아 보고서를 파일로 바로 저장했어요. 새 탭으로 보려면 이 사이트의 팝업을 허용해 주세요.";

/** 대화창 [보고서]: 이 대화의 답 중 고른 것만으로 보고서를 만든다 (한 대화의 답만, 최대 MAX_ITEMS개) */
export function ReportSheet({ sessionId, answers, onClose }: Props) {
  const picks = answers.map(pickable);
  const [chosen, setChosen] = useState<number[]>([]);
  const [making, setMaking] = useState(false);
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
    const order = picks.map((p) => p.turnId).filter((t): t is number => t !== null && chosen.includes(t));
    try {
      const report = await createReport(sessionId, order);
      if (report.status !== "ready") {
        setMessage("보고서 문장을 만들지 못했어요. 잠시 후 다시 시도해 주세요.");
        return;
      }
      setMessage(openBundleReport(report) === "saved" ? SAVED : OPENED);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "보고서를 만들지 못했어요. 다시 시도해 주세요.");
    } finally {
      setMaking(false);
    }
  }

  return (
    <Sheet title="보고서 만들기" onClose={onClose}>
      <p className="sheet-text">
        이 대화의 답 중 보고서에 담을 것을 고르세요. {MAX_ITEMS}개까지 고를 수 있어요.
      </p>
      <ul className="report-picks">
        {picks.map((p, i) => {
          const picked = p.turnId !== null && chosen.includes(p.turnId);
          const off = p.reason !== null || p.turnId === null;
          return (
            <li key={p.turnId ?? `off-${i}`}>
              <label className={off ? "report-pick off" : "report-pick"}>
                <input
                  type="checkbox"
                  checked={picked}
                  disabled={off || making || (full && !picked)}
                  onChange={() => p.turnId !== null && toggle(p.turnId)}
                />
                <span className="report-pick-text">
                  <span className="report-pick-q">{p.question || "질문"}</span>
                  <span className="report-pick-a">{p.reason ?? p.answer}</span>
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
