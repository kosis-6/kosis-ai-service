// 수치 질문의 답 한 건 (POST /ask): 알림 + 답변 문장 + 적용한 조건 + 차트 + 수치·계산식 + 출처
import { lazy, Suspense } from "react";
import type { Applied, AskResponse } from "../api/types";
import { safeUrl, splitLinks } from "./format";

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

export function Answer({ data }: { data: AskResponse }) {
  const notice = data.notice && <p className="answer-notice">{data.notice}</p>;
  // 답하지 못한 경우(연도 되묻기, 자료 준비 중, 범위 밖)는 서버 안내를 그대로 보여 준다
  if (data.status !== "ok") {
    return (
      <>
        {notice}
        <div className="bubble">
          <Linked text={data.answer} />
        </div>
      </>
    );
  }
  const { lead, detail } = splitAnswer(data.answer);
  return (
    <>
      {notice}
      <div className="bubble">{lead}</div>
      <Conditions applied={data.applied ?? []} />
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
      {data.sources.length > 0 && (
        <ul className="sources">
          {data.sources.map((s) => (
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
