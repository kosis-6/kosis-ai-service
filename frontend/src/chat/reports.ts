import type { Applied, AskResponse, Chart, Source } from "../api/types";
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

export type Pick = { turnId: number | null; question: string; answer: string; reason: string | null };

// 앞 답을 다시 쓴 응답 (차트 · 요약 · 자세히 · 출처): 새로 계산한 결과가 아니다
const REUSED = new Set(["chart", "summary", "detail", "source"]);

function lead(answer: string): string {
  const first = answer.split("\n").find((line) => line.trim()) ?? "";
  return first.length > 90 ? `${first.slice(0, 89)}…` : first;
}

function whyNot(data: AskResponse): string {
  if (data.status !== "ok") return "수치로 답하지 않은 답이에요";
  if (data.request_type && REUSED.has(data.request_type)) return "앞 답의 차트·요약만 다시 보여 준 답이에요";
  return "검증된 수치로 답한 결과만 담을 수 있어요";
}

/** 이 대화의 답 하나를 보고서에 고를 수 있는가. 고를 수 없으면 reason에 이유 */
export function pickable(data: AskResponse): Pick {
  const reason = !data.reportable
    ? whyNot(data)
    : data.turn_id == null
      ? "저장된 답이 아니라 담을 수 없어요"
      : null;
  return { turnId: data.turn_id ?? null, question: data.question ?? "", answer: lead(data.answer), reason };
}

/** 고른 답(한 대화의 답만)으로 보고서를 만들고 문장 생성까지 한 번에 */
export function createReport(sessionId: string | null, turnIds: number[]): Promise<Report> {
  return postJson<Report>("/reports", { session_id: sessionId, turn_ids: turnIds, generate: true });
}
