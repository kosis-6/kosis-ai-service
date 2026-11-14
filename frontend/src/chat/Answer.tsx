// 질문의 답 한 건 (POST /ask)
//   수치로 답함: 알림 + 답변 문장 + 적용한 조건 + 차트 + 수치·계산식 + 통계표 카드 + 출처
//   통계표만 안내함(status "tables"): 통계표 찾기와 같은 말풍선·표 카드·선택지 (Reply)
import { lazy, Suspense, useState } from "react";
import type { Applied, AskResponse, Parsed, Recommendation, SearchResponse } from "../api/types";
import { safeUrl, splitLinks } from "./format";
import { Reply, TopCard, type Send } from "./Reply";
import { canReport, downloadReport } from "./report";

// 차트 라이브러리(Recharts)는 차트가 있는 답을 처음 그릴 때 내려받는다
const ChartCard = lazy(() => import("./Chart"));

/** 서버 답변을 나눈다: 첫 문단은 말풍선에, 나머지(수치 목록·계산식·주의)는 접어 둔다.
 * 출처·통계표 줄은 아래 출처 칸이 대신하므로 뺀다 */
export function splitAnswer(answer: string): { lead: string; detail: string } {
  const [lead, ...rest] = answer.split("\n\n");
  const detail = rest
    .join("\n\n")
    .split("\n")
    .filter((line) => !line.startsWith("출처:") && !line.startsWith("통계표:"))
    .join("\n")
    .trim();
  return { lead, detail };
}

function Linked({ text }: { text: string }) {
  return (
    <>
      {splitLinks(text).map((part, i) =>
        part.href ? (
          <a key={i} href={part.href} target="_blank" rel="noopener">
            {part.text}
          </a>
        ) : (
          part.text
        ),
      )}
    </>
  );
}

const SOURCE_WORDS: Record<string, string> = { inherited: "이어받음", default: "기본값" };

// 이번 답에 적용한 조건. 사용자가 말하지 않은 조건(앞 질문에서 이어받음, 기본값)은 그렇다고 적는다
function Conditions({ applied }: { applied: Applied[] }) {
  if (!applied.length) return null;
  return (
    <ul className="conditions" aria-label="적용한 조건">
      {applied.map((c) => (
        <li key={c.name} className={c.source === "user" ? undefined : "soft"}>
          <span className="condition-name">{c.name}</span>
          {c.value}
          {SOURCE_WORDS[c.source] && <span className="condition-source">{SOURCE_WORDS[c.source]}</span>}
        </li>
      ))}
    </ul>
  );
}

type Props = {
  data: AskResponse;
  busy: boolean;
  onSend: Send;
  onOthers: (recs: Recommendation[], parsed: Parsed) => void;
  onAskAgain: () => void;
};

function ReportButton({ data }: { data: AskResponse }) {
  const [error, setError] = useState<string | null>(null);
  return (
    <div>
      <button type="button" className="btn secondary card-link" onClick={() => {
        try {
          downloadReport(data);
          setError(null);
        } catch {
          setError("보고서를 저장하지 못했습니다. 다시 시도해 주세요.");
        }
      }}>
        보고서 저장 (HTML)
      </button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

export function Answer({ data, busy, onSend, onOthers, onAskAgain }: Props) {
  const notice = data.notice && <p className="answer-notice">{data.notice}</p>;
  const tables = data.tables;
  const clarification = data.clarification;
  const options = clarification?.options ?? [];

  // 수치 없이 통계표만 안내한 답 (지원하지 않는 지표 등): 표 카드·선택지를 통계표 찾기 화면과 같게 그린다
  if (data.status === "tables" && tables) {
    const search: SearchResponse = {
      session_id: data.session_id ?? "",
      status: tables.status ?? "ok",
      answer: data.answer,
      parsed: tables.parsed ?? {},
      clarify: tables.clarify ?? null,
      recommendations: tables.recommendations ?? [],
      notice: tables.notice ?? null,
    };
    return (
      <>
        {notice}
        <Reply
          data={search}
          busy={busy}
          onSend={onSend}
          onOthers={onOthers}
          onDebug={() => undefined}
          onAskAgain={onAskAgain}
        />
      </>
    );
  }

  // 답하지 못한 경우(연도 되묻기, 자료 준비 중, 범위 밖)는 서버 안내를 그대로 보여 준다
  if (data.status !== "ok") {
    return (
      <>
        {notice}
        <div className="bubble">
          <Linked text={data.answer} />
        </div>
        {clarification && options.length > 0 && (
          <div className="choices" role="group" aria-label="집계 범위 선택">
            {options.map((option) => (
              <button
                key={option}
                type="button"
                className="btn secondary"
                disabled={busy}
                onClick={() => onSend(option, {
                  clarification_kind: clarification.kind,
                  clarification_value: option,
                })}
              >
                {option}
              </button>
            ))}
          </div>
        )}
      </>
    );
  }

  const { lead, detail } = splitAnswer(data.answer);
  const cards = tables?.recommendations ?? [];
  // 표 카드로 보여 준 표는 출처 목록에서 뺀다 (같은 표의 KOSIS 버튼이 두 번 나오지 않게)
  const carded = new Set(cards.map((c) => c.tbl_id));
  const sources = data.sources.filter((s) => !s.tbl_id || !carded.has(s.tbl_id));
  return (
    <>
      {notice}
      <div className="bubble">{lead}</div>
      <Conditions applied={data.applied ?? []} />
      {canReport(data) && <ReportButton data={data} />}
      {data.chart && (
        <Suspense fallback={<div className="chart-card chart-loading">차트를 그리고 있어요</div>}>
          <ChartCard chart={data.chart} />
        </Suspense>
      )}
      {detail && data.request_type !== "chart" && data.request_type !== "summary" && (
        // 차트가 없으면(값 하나) 계산식이 답의 일부라 펼쳐 둔다
        <details className="answer-detail" open={!data.chart}>
          <summary>수치와 계산식 보기</summary>
          <p>
            <Linked text={detail} />
          </p>
        </details>
      )}
      {cards.length > 0 && <TopCard r={cards[0]} badge="이 수치의 통계표" />}
      {cards.length > 1 && (
        <button type="button" className="btn secondary card-link" onClick={() => onOthers(cards.slice(1), {})}>
          {`함께 쓴 통계표 ${cards.length - 1}개 더 보기  ⌄`}
        </button>
      )}
      {sources.length > 0 && (
        <ul className="sources">
          {sources.map((s) => (
            <li key={s.name}>
              <span className="source-label">출처</span>
              <span className="source-name">{s.name}</span>
              <span className="source-basis">{s.basis}</span>
              {s.url && safeUrl(s.url) && (
                <a className="source-link" href={safeUrl(s.url)} target="_blank" rel="noopener">
                  {"KOSIS에서 보기  ↗"}
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
