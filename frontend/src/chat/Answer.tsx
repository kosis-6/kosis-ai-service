import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { Applied, AskResponse, ChartView, Parsed, Recommendation, SearchResponse } from "../api/types";
import { Icon } from "../components/Icon";
import { safeUrl, splitLinks } from "./format";
import { Reply, TopCard, type Send } from "./Reply";
import { getReport, type Report } from "./reports";

const ChartCard = lazy(() => import("./Chart"));

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
  /** 대화창 [보고서] 시트를 연다 (로그인 · 저장된 대화일 때만 넘어온다) */
  onReport?: () => void;
  /** 보고서 에이전트가 만든 보고서를 앱 안에서 본다 */
  onShowReport?: (report: Report) => void;
  onChartViewChange?: (view: ChartView) => void;
  onSend: Send;
  onOthers: (recs: Recommendation[], parsed: Parsed) => void;
  onAskAgain: () => void;
};

/** "지금까지 본 ○○ 통계로 보고서를 만들까요?" 칩. [만들기]는 말로 보내 보고서 에이전트가,
 * [고르기]는 대화창 [보고서] 시트가 맡는다 */
function ReportOffer({ data, busy, onReport, onSend }: { data: AskResponse; busy: boolean; onReport?: () => void; onSend: Send }) {
  const [closed, setClosed] = useState(false);
  const offer = data.report_offer;
  if (!offer || !onReport || closed) return null;
  return (
    <div className="report-offer" role="group" aria-label="보고서 제안">
      <p>
        지금까지 본 <strong>{offer.topic}</strong> 통계 {offer.count}개로 보고서를 만들까요?
      </p>
      <div className="choices">
        <button type="button" className="btn secondary" disabled={busy} onClick={() => { setClosed(true); onSend("보고서 만들어줘"); }}>
          만들기
        </button>
        <button type="button" className="btn secondary" onClick={onReport}>
          고르기
        </button>
        <button type="button" className="text-btn" disabled={busy} onClick={() => { setClosed(true); onSend("괜찮아요"); }}>
          괜찮아요
        </button>
      </div>
    </div>
  );
}

function ReportLink({ report, id, onShow }: { report?: Report; id?: number; onShow: (report: Report) => void }) {
  const request = useRef<AbortController | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => () => request.current?.abort(), []);

  async function open() {
    if (report) {
      onShow(report);
      return;
    }
    if (id == null || loading) return;
    const pending = new AbortController();
    request.current = pending;
    setLoading(true);
    setError(false);
    try {
      const loaded = await getReport(id, pending.signal);
      if (!pending.signal.aborted) {
        if (loaded.status === "ready") onShow(loaded);
        else setError(true);
      }
    } catch {
      if (!pending.signal.aborted) setError(true);
    } finally {
      if (!pending.signal.aborted) setLoading(false);
    }
  }

  return (
    <>
      <button type="button" className="btn secondary card-link" disabled={loading} onClick={() => void open()}>
        보고서 보기
      </button>
      {error && <p role="alert">보고서를 열지 못했어요. 다시 시도해 주세요.</p>}
    </>
  );
}

export function Answer({ data, busy, onReport, onShowReport, onChartViewChange, onSend, onOthers, onAskAgain }: Props) {
  const notice = data.notice && <p className="answer-notice">{data.notice}</p>;
  const tables = data.tables;
  const clarification = data.clarification;
  const options = clarification?.options ?? [];
  const report = data.report as Report | undefined;
  const reportId = data.report_ref?.status === "ready" ? data.report_ref.id : undefined;
  const reportButton = onShowReport && (report?.status === "ready" || reportId != null) && (
    <ReportLink report={report?.status === "ready" ? report : undefined} id={reportId} onShow={onShowReport} />
  );

  if ((data.status === "tables" || data.result?.status === "tables") && tables) {
    const search: SearchResponse = {
      session_id: data.session_id ?? "",
      status: tables.status ?? "ok",
      answer: data.answer,
      parsed: tables.parsed ?? {},
      clarify: tables.clarify ?? null,
      recommendations: tables.recommendations ?? [],
      together: tables.together,
      notice: tables.notice ?? null,
    };
    return (
      <>
        {notice}
        <Reply
          data={search}
          fullAnswer={Boolean(data.talk_summary && search.clarify)}
          busy={busy}
          onSend={onSend}
          onOthers={onOthers}
          onAskAgain={onAskAgain}
        />
        {reportButton}
      </>
    );
  }

  if (data.status !== "ok") {
    return (
      <>
        {notice}
        <div className="bubble">
          <Linked text={data.answer} />
        </div>
        {reportButton}
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

  const { lead, detail } = data.talk_summary ? { lead: data.answer, detail: "" } : splitAnswer(data.answer);
  const cards = tables?.recommendations ?? [];
  const carded = new Set(cards.map((c) => c.tbl_id));
  const sources = data.sources.filter((s) => !s.tbl_id || !carded.has(s.tbl_id));
  return (
    <>
      {notice}
      <div className="bubble">
        <Linked text={lead} />
      </div>
      <Conditions applied={data.applied ?? []} />
      {reportButton}
      <ReportOffer data={data} busy={busy} onReport={onReport} onSend={onSend} />
      {data.chart && (
        <Suspense fallback={<div className="chart-card chart-loading">차트를 그리고 있어요</div>}>
          <ChartCard chart={data.chart} onViewChange={onChartViewChange} />
        </Suspense>
      )}
      {detail && data.request_type !== "chart" && data.request_type !== "summary" && (
        <details className="answer-detail" open={!data.chart || data.request_type === "detail"}>
          <summary>수치와 계산식 보기</summary>
          <p>
            <Linked text={detail} />
          </p>
        </details>
      )}
      {cards.length > 0 && <TopCard r={cards[0]} badge="이 수치의 통계표" />}
      {cards.length > 1 && (
        <button type="button" className="btn secondary card-link" onClick={() => onOthers(cards.slice(1), {})}>
          {`함께 쓴 통계표 ${cards.length - 1}개 더 보기`}
          <Icon name="chevronDown" />
        </button>
      )}
      {sources.length > 0 && (
        <ul className="sources">
          {sources.map((s) => (
            <li key={s.name}>
              <span className="source-label">출처</span>
              <span className="source-name">
                {s.tbl_id ? s.name.replace(` (${s.tbl_id})`, "") : s.name}
                {s.tbl_id && <code className="tbl-id">{s.tbl_id}</code>}
              </span>
              <span className="source-basis">{s.basis}</span>
              {s.url && safeUrl(s.url) && (
                <a className="source-link" href={safeUrl(s.url)} target="_blank" rel="noopener">
                  KOSIS에서 보기
                  <Icon name="external" />
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
