
const KST = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function when(iso: string): string {
  const parts = Object.fromEntries(KST.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${parts.month}.${parts.day} ${parts.hour}:${parts.minute}`;
}

export function day(date: string): string {
  const [, month, d] = date.split("-");
  return `${month}.${d}`;
}

export const count = (n: number): string => n.toLocaleString("ko-KR");

export function seconds(ms: number | null | undefined): string {
  return ms == null ? "" : `${(ms / 1000).toFixed(1)}초`;
}

export type Tone = "good" | "bad" | "plain";
type Label = { text: string; tone: Tone };

const STATUS: Record<string, Label> = {
  ok: { text: "답함", tone: "good" },
  tables: { text: "통계표 안내", tone: "plain" },
  chat: { text: "인사", tone: "plain" },
  unclear: { text: "되물음", tone: "plain" },
  clarify_question: { text: "질문 되묻기", tone: "plain" },
  ambiguous_region: { text: "지역 되묻기", tone: "plain" },
  ambiguous: { text: "되묻기", tone: "plain" },
  clarify: { text: "항목 되묻기", tone: "plain" },
  partial_match: { text: "일부 일치", tone: "plain" },
  out_of_range: { text: "기간 밖", tone: "bad" },
  out_of_scope: { text: "범위 밖", tone: "bad" },
  not_statistics: { text: "통계 아님", tone: "bad" },
  unknown_region: { text: "모르는 지역", tone: "bad" },
  region_not_in_year: { text: "그 해에 없는 지역", tone: "bad" },
  missing_data: { text: "자료 없음", tone: "bad" },
  no_region_level: { text: "없는 지역 단위", tone: "bad" },
  no_joint_table: { text: "함께 집계한 통계 없음", tone: "bad" },
  band_mismatch: { text: "맞는 구간 없음", tone: "bad" },
  data_loading: { text: "자료 준비 중", tone: "bad" },
  parse_error: { text: "해석 실패", tone: "bad" },
  error: { text: "계산 오류", tone: "bad" },
  unknown_indicator: { text: "사전에 없는 지표", tone: "bad" },
  not_supported_question: { text: "지원하지 않는 질문", tone: "bad" },
  rate_limited: { text: "요청 한도 초과", tone: "bad" },
};

export function statusLabel(
  status: string | null | undefined,
  search = false,
  localizeUnknown = false,
): Label {
  if (!status) return { text: "기록 없음", tone: "plain" };
  if (search && status === "ok") return { text: "찾음", tone: "good" };
  return STATUS[status] ?? { text: localizeUnknown ? "분류되지 않은 상태" : status, tone: "plain" };
}

const REQUEST: Record<string, string> = {
  new: "새 질문",
  change: "조건 변경",
  chart: "차트 바꾸기",
  summary: "요약",
  detail: "자세히",
  source: "출처",
  reply: "되묻기 답",
  chat: "인사",
  unclear: "되물음",
};

const FLOW_RESULT: Record<string, Label> = {
  ok: { text: "성공", tone: "good" },
  HCX: { text: "HCX", tone: "plain" },
  캐시: { text: "캐시", tone: "good" },
  씀: { text: "캐시 씀", tone: "good" },
  없음: { text: "캐시 없음", tone: "plain" },
  건너뜀: { text: "건너뜀", tone: "plain" },
  시작: { text: "시작", tone: "plain" },
  되묻기: { text: "되묻기", tone: "plain" },
  "못 함": { text: "못 함", tone: "bad" },
  uncovered: { text: "남은 낱말", tone: "bad" },
  no_cell: { text: "맞는 칸 없음", tone: "bad" },
  not_count: { text: "건수가 아닌 값", tone: "bad" },
  "-": { text: "결과 없음", tone: "plain" },
};

export function flowLabel(result: string): Label {
  return FLOW_RESULT[result] ?? STATUS[result] ?? { text: "분류되지 않은 결과", tone: "bad" };
}

type Step = { stage: string; result: string; detail?: string };

export function stuckStep<T extends Step>(flow: T[] | null | undefined): T | null {
  const steps = flow ?? [];
  const final = steps.findLast((s) => s.stage === "최종 답");
  if (!final || final.result === "ok") return null;
  return steps.findLast((s) => s.stage !== "최종 답" && flowLabel(s.result).tone === "bad") ?? null;
}

export function requestLabel(type: string | null | undefined, localizeUnknown = false): string {
  return type ? (REQUEST[type] ?? (localizeUnknown ? "분류되지 않은 요청" : type)) : "";
}

type Person = { user_id?: number | null; user_email?: string | null; user_nickname?: string | null };

export function person(p: Person): string {
  if (p.user_nickname) return p.user_nickname;
  if (p.user_email) return p.user_email;
  return p.user_id == null ? "비로그인 사용자" : `사용자 ${p.user_id}`;
}

const CONDITION: [string, string][] = [
  ["indicator", "지표"],
  ["indicators", "지표"],
  ["type", "질문 종류"],
  ["regions", "지역"],
  ["sido", "시도"],
  ["level", "지역 단위"],
  ["year", "연도"],
  ["start_year", "시작 연도"],
  ["end_year", "끝 연도"],
  ["sex", "성별"],
  ["age_group", "연령"],
  ["by", "나누는 기준"],
  ["top", "개수"],
  ["ascending", "순서"],
];
const KIND: Record<string, string> = {
  value: "값",
  trend: "추이",
  compare: "비교",
  rank: "순위",
  composition: "구성",
  causal: "원인 질문",
  other: "통계 질문 이외",
  parse_error: "해석 실패",
};

function show(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "없음";
  if (key === "ascending") return value ? "낮은 순" : "높은 순";
  if (key === "type" && typeof value === "string") return KIND[value] ?? "분류되지 않은 유형";
  if (Array.isArray(value)) return value.length ? value.map((v) => show("", v)).join(", ") : "없음";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

type Conditions = Record<string, unknown> | null | undefined;
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export type Change = { name: string; from: string; to: string };

export function changes(before: Conditions, after: Conditions): Change[] {
  if (!isRecord(before) || !isRecord(after)) return [];
  const out: Change[] = [];
  for (const [key, name] of CONDITION) {
    if (JSON.stringify(before[key] ?? null) === JSON.stringify(after[key] ?? null)) continue;
    out.push({ name, from: show(key, before[key]), to: show(key, after[key]) });
  }
  return out;
}

export function conditionLine(after: Conditions): string {
  if (!isRecord(after)) return "";
  const parts: string[] = [];
  const indicator = after.indicator ?? after.indicators;
  if (indicator) parts.push(show("indicator", indicator));
  if (Array.isArray(after.regions) && after.regions.length) parts.push(show("regions", after.regions));
  const { start_year: start, end_year: end, year } = after;
  if (start && end && start !== end) parts.push(`${start}~${end}`);
  else if (year ?? end) parts.push(`${year ?? end}`);
  if (typeof after.sex === "string" && after.sex !== "계") parts.push(after.sex);
  if (typeof after.type === "string") parts.push(show("type", after.type));
  return parts.join(" · ");
}

export function filled(conditions: unknown): [string, string][] {
  if (!isRecord(conditions)) return [];
  return Object.entries(conditions)
    .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => [k, show("", v)]);
}
