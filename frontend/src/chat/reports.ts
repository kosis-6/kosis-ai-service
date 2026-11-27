import type { Applied, AskResponse, Chart, ChartView, Source } from "../api/types";
import { authFetch } from "../auth/client";
import { postJson } from "./api";

// 보고서 하나에 담을 수 있는 답 수 (서버 app/services/reports.py MAX_ITEMS와 같다)
export const MAX_ITEMS = 8;

// 보고서 API(/reports) 응답 중 화면이 쓰는 부분 (app/schemas/reports.py, 생성 결과는 report_writer.write)
export type ReportLine = { text: string; refs?: string[] };
export type ReportResult = {
  id: string;
  turn_id: number;
  question?: string | null;
  label?: string | null;
  value_text?: string | null;
  figure?: Figure | null;
  sources?: Source[];
  chart?: Chart | null;
};
export type ReportContent = {
  title?: string;
  purpose?: string | null;
  summary?: ReportLine[];
  analysis?: ReportLine[];
  limits?: string[];
  warnings?: string[];
  items?: ReportResult[];
  error?: string;
};
// 대표 수치 (app/services/figure.py): 값 하나면 value, 추이 · 비교 · 순위 · 구성비면 rows
export type FigureRow = { label: string; value: number | null; rank?: number | null; count?: number | null };
export type Figure = {
  kind?: string;
  label?: string;
  value?: number | null;
  unit?: string;
  rows?: FigureRow[];
  operands?: { label: string; value: number | null; unit: string }[];
};
// 담을 때 복사한 답 화면 내용 중 보고서가 쓰는 것
export type ReportSnapshot = { applied?: Applied[]; figure?: Figure | null };
export type Report = {
  id: number;
  session_id: string | null;
  title: string;
  purpose: string | null;
  status: "draft" | "ready" | "failed";
  content: ReportContent | null;
  items: { turn_id: number; snapshot?: ReportSnapshot }[];
};

export type Pick = { turnId: number; question: string; answer: string };

function lead(answer: string): string {
  const first = answer.split("\n").find((line) => line.trim()) ?? "";
  return first.length > 90 ? `${first.slice(0, 89)}…` : first;
}

/** 이 대화의 답 중 보고서에 고를 수 있는 답만: 검증된 수치로 답했고(reportable) 저장된(turn_id) 답.
 *  요약 · 차트만 다시 보여 준 답, 표 안내, 실패한 답은 목록에 넣지 않는다
 *  (요약은 보고서가 고른 답으로 직접 만든다. 고를 수 없는 답을 꺼 두고 보여 주면 '이미 쓴 답'처럼 읽혔다) */
export function pickable(answers: AskResponse[]): Pick[] {
  return answers.flatMap((a) =>
    a.reportable && a.turn_id != null ? [{ turnId: a.turn_id, question: a.question ?? "", answer: lead(a.answer) }] : [],
  );
}

/** 고른 답(한 대화의 답만)으로 보고서를 만들고 문장 생성까지 한 번에 */
export function createReport(sessionId: string | null, turnIds: number[], chartViews: Record<number, ChartView>): Promise<Report> {
  return postJson<Report>("/reports", {
    session_id: sessionId, turn_ids: turnIds, generate: true, chart_views: chartViews,
  });
}

export async function getReport(id: number, signal: AbortSignal): Promise<Report> {
  const res = await authFetch(`/reports/${id}`, { signal });
  if (!res.ok) throw new Error("보고서를 불러오지 못했어요.");
  return (await res.json()) as Report;
}
