// 답변 한 건: 말풍선 + 알림 + 추천 표 카드 + 되묻기 선택지 (POST /search 응답의 status 별)
import { useState } from "react";
import type { Axis, FamilyChoice, Parsed, Recommendation, SearchResponse } from "../api/types";
import {
  AXIS_LABELS,
  NO_RESULT_TITLES,
  bubbleText,
  checkPills,
  defaultOption,
  memberAnswer,
  metaLine,
  noticeLines,
  otherVersions,
  pathText,
  safeUrl,
  splitLinks,
} from "./format";

export type Send = (question: string, selections?: Record<string, string>) => void;

function KosisLink({ url, className = "" }: { url: string; className?: string }) {
  return (
    <a className={`btn primary ${className}`.trim()} href={safeUrl(url)} target="_blank" rel="noopener">
      {"KOSIS에서 보기  ↗"}
    </a>
  );
}

function CheckPills({ r, parsed }: { r: Recommendation; parsed?: Parsed }) {
  return (
    <>
      {checkPills(r, parsed).map(([text, ok]) => (
        <span key={text} className={ok ? "pill on" : "pill"}>
          {text}
        </span>
      ))}
    </>
  );
}

export function TopCard({ r, parsed }: { r: Recommendation; parsed?: Parsed }) {
  const path = pathText(r);
  const others = otherVersions(r.family);
  return (
    <article className="card">
      <span className="card-badge">추천 통계표</span>
      <h3>{r.tbl_nm}</h3>
      <p className="card-meta">{metaLine(r)}</p>
      {path && <p className="card-path">{path}</p>}
      <div className="pills">
        <span className="pill on">{`제공 기간  ${r.period}년`}</span>
        <CheckPills r={r} parsed={parsed} />
      </div>
      {others && <p className="card-versions">{others}</p>}
      <KosisLink url={r.url} className="card-link" />
    </article>
  );
}

export function AltCard({ r, parsed }: { r: Recommendation; parsed?: Parsed }) {
  const hasChecks = Object.keys(r.checks ?? {}).length > 0;
  return (
    <article className="alt-card">
      <h3>{r.tbl_nm}</h3>
      <p className="card-meta">{metaLine(r, " · ")}</p>
      {hasChecks && (
        <div className="pills">
          <CheckPills r={r} parsed={parsed} />
        </div>
      )}
      {r.notes && r.notes.length > 0 && (
        <ul className="card-notes">
          {r.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      <KosisLink url={r.url} />
    </article>
  );
}

// 대상·성별·판을 고르는 되묻기. 한 번 보내면 다시 누르지 못하게 잠근다
function MemberChoices({ axes, busy, onSend }: { axes: Axis[]; busy: boolean; onSend: Send }) {
  const [picked, setPicked] = useState<Record<string, string>>(() => {
    const preset: Record<string, string> = {};
    for (const a of axes) {
      const o = defaultOption(a);
      if (o) preset[a.axis] = o;
    }
    return preset;
  });
  const [sent, setSent] = useState(false);

  function toggle(axis: string, option: string) {
    setPicked((prev) => {
      const next = { ...prev };
      if (next[axis] === option) delete next[axis];
      else next[axis] = option;
      return next;
    });
  }
  function send(text: string, selections?: Record<string, string>) {
    if (busy) return;
    setSent(true);
    onSend(text, selections);
  }

  return (
    <div className="choices" inert={sent}>
      {axes.map((a) => (
        <div className="axis" key={a.axis}>
          <span className="axis-label">{AXIS_LABELS[a.axis] || a.axis}</span>
          <div className="pills">
            {a.options.map((o) => {
              const on = picked[a.axis] === o;
              return (
                <button
                  type="button"
                  key={o}
                  className={on ? "pill on" : "pill"}
                  aria-pressed={on}
                  onClick={() => toggle(a.axis, o)}
                >
                  {a.axis === "edition" ? `${o}년` : o}
                </button>
              );
            })}
          </div>
        </div>
      ))}
      <div className="actions">
        <button
          type="button"
          className="btn primary"
          disabled={Object.keys(picked).length === 0}
          onClick={() => {
            const { text, selections } = memberAnswer(picked);
            send(text, selections);
          }}
        >
          이렇게 찾기
        </button>
        <button type="button" className="btn quiet" onClick={() => send("아무거나")}>
          아무거나
        </button>
      </div>
    </div>
  );
}

// 주제가 같은 표 묶음이 여럿일 때 하나를 고르는 되묻기
function FamilyChoices({ families, busy, onSend }: { families: FamilyChoice[]; busy: boolean; onSend: Send }) {
  const [sent, setSent] = useState(false);
  return (
    <div className="choices" inert={sent}>
      <span className="axis-label">찾는 자료를 골라 주세요</span>
      <div className="family-list">
        {families.map((f, i) => (
          <button
            type="button"
            className="family-btn"
            key={f.family_id}
            onClick={() => {
              if (busy) return;
              setSent(true);
              onSend(f.tbl_nm, { family_id: f.family_id });
            }}
          >
            {`${"①②③④⑤"[i] || i + 1} ${f.tbl_nm}`}
            <small>{`${f.survey_nm} · ${f.period}년`}</small>
          </button>
        ))}
      </div>
    </div>
  );
}

function Notice({ data }: { data: SearchResponse }) {
  return (
    <div className="notice">
      <p className="notice-title">
        {data.status === "partial_match" ? "질문의 조건을 모두 충족하는 표를 찾지 못했어요." : "확인이 필요해요"}
      </p>
      <p className="notice-text">{noticeLines(data)}</p>
    </div>
  );
}

type Props = {
  data: SearchResponse;
  busy: boolean;
  onSend: Send;
  onOthers: (recs: Recommendation[], parsed: Parsed) => void;
  onDebug: (data: SearchResponse) => void;
  onAskAgain: () => void;
};

export function Reply({ data, busy, onSend, onOthers, onDebug, onAskAgain }: Props) {
  const recs = data.recommendations ?? [];
  const title = NO_RESULT_TITLES[data.status];
  return (
    <>
      <div className="bubble">
        {title && <p className="bubble-title">{title}</p>}
        {splitLinks(bubbleText(data)).map((part, i) =>
          part.href ? (
            <a key={i} href={part.href} target="_blank" rel="noopener">
              {part.text}
            </a>
          ) : (
            part.text
          ),
        )}
      </div>
      {data.notice && <Notice data={data} />}
      {recs.length > 0 && <TopCard r={recs[0]} parsed={data.parsed} />}
      {recs.length > 1 && (
        <button type="button" className="btn secondary card-link" onClick={() => onOthers(recs.slice(1), data.parsed)}>
          {`다른 후보 ${recs.length - 1}개 보기  ⌄`}
        </button>
      )}
      {data.clarify?.level === "member" && <MemberChoices axes={data.clarify.axes} busy={busy} onSend={onSend} />}
      {data.clarify?.level === "family" && (
        <FamilyChoices families={data.clarify.families} busy={busy} onSend={onSend} />
      )}
      {title && (
        <button type="button" className="btn primary ask-again" onClick={onAskAgain}>
          다른 통계 질문하기
        </button>
      )}
      {data.debug && (
        <button type="button" className="text-btn debug-link" onClick={() => onDebug(data)}>
          {"디버그 · 검색 과정 보기  ›"}
        </button>
      )}
    </>
  );
}
