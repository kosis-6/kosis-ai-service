// 수치 질문의 답 한 건 (POST /ask): 답변 문장 + 차트 + 수치·계산식 + 출처
import { lazy, Suspense } from "react";
import type { AskResponse } from "../api/types";
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

export function Answer({ data }: { data: AskResponse }) {
  // 답하지 못한 경우(연도 되묻기, 자료 준비 중, 범위 밖)는 서버 안내를 그대로 보여 준다
  if (data.status !== "ok") {
    return (
      <div className="bubble">
        <Linked text={data.answer} />
      </div>
    );
  }
  const { lead, detail } = splitAnswer(data.answer);
  return (
    <>
      <div className="bubble">{lead}</div>
      {data.chart && (
        <Suspense fallback={<div className="chart-card chart-loading">차트를 그리고 있어요</div>}>
          <ChartCard chart={data.chart} />
        </Suspense>
      )}
      {detail && (
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
