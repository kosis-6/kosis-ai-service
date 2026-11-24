// 관리자 화면에서 기록을 읽기 쉽게 바꾸는 함수들 (화면 없이 테스트한다)

const KST = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** ISO 시각 → 한국 시간 "11.15 17:37" */
export function when(iso: string): string {
  const parts = Object.fromEntries(KST.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${parts.month}.${parts.day} ${parts.hour}:${parts.minute}`;
}

/** "2026-11-15" → "11.15" */
export function day(date: string): string {
  const [, month, d] = date.split("-");
  return `${month}.${d}`;
}

export const count = (n: number): string => n.toLocaleString("ko-KR");

/** 걸린 시간(ms) → "4.2초". 기록에 없으면 빈 글자 */
export function seconds(ms: number | null | undefined): string {
  return ms == null ? "" : `${(ms / 1000).toFixed(1)}초`;
}

export type Tone = "good" | "bad" | "plain";
type Label = { text: string; tone: Tone };

// /ask 와 통계표 찾기의 status. 모르는 값은 코드 그대로 보여 준다
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
};

/** search: 통계표 찾기의 status 면 true ("ok" 가 수치로 답한 것이 아니라 표를 찾은 것이다) */
export function statusLabel(status: string | null | undefined, search = false): Label {
  if (!status) return { text: "기록 없음", tone: "plain" };
  if (search && status === "ok") return { text: "찾음", tone: "good" };
  return STATUS[status] ?? { text: status, tone: "plain" };
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

// 처리 흐름 한 단계의 결과 (app/services/flow.py). 계산 단계는 /ask status 를 그대로 쓴다
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
  unknown_indicator: { text: "사전에 없는 지표", tone: "bad" },
};

export function flowLabel(result: string): Label {
  return FLOW_RESULT[result] ?? STATUS[result] ?? { text: result, tone: "bad" };
}

type Step = { stage: string; result: string; detail?: string };

/** 최종 답이 성공이 아니면 마지막으로 막힌 단계 (펼치기 전에 보여 준다). 성공했으면 null */
export function stuckStep<T extends Step>(flow: T[] | null | undefined): T | null {
  const steps = flow ?? [];
  const final = steps.findLast((s) => s.stage === "최종 답");
  if (!final || final.result === "ok") return null;
  return steps.findLast((s) => s.stage !== "최종 답" && flowLabel(s.result).tone === "bad") ?? null;
}

export function requestLabel(type: string | null | undefined): string {
  return type ? (REQUEST[type] ?? type) : "";
}

type Person = { user_id?: number | null; user_email?: string | null; user_nickname?: string | null };

/** 누가 남긴 기록인지. 로그인하지 않았으면 "비로그인 사용자", 지워진 계정이면 번호만 */
export function person(p: Person): string {
  if (p.user_nickname) return p.user_nickname;
  if (p.user_email) return p.user_email;
  return p.user_id == null ? "비로그인 사용자" : `사용자 ${p.user_id}`;
}

// 조건 이름 (backend/app/services/ask_session.py 의 conditions). 여기 없는 칸은 화면에 내지 않는다
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
};

function show(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "없음";
  if (key === "ascending") return value ? "낮은 순" : "높은 순";
  if (key === "type" && typeof value === "string") return KIND[value] ?? value;
  if (Array.isArray(value)) return value.length ? value.map((v) => show("", v)).join(", ") : "없음";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

type Conditions = Record<string, unknown> | null | undefined;
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export type Change = { name: string; from: string; to: string };

/** 이 턴에서 바뀐 조건만. 앞 조건이 없으면(첫 질문) 빈 목록 */
export function changes(before: Conditions, after: Conditions): Change[] {
  if (!isRecord(before) || !isRecord(after)) return [];
  const out: Change[] = [];
  for (const [key, name] of CONDITION) {
    if (JSON.stringify(before[key] ?? null) === JSON.stringify(after[key] ?? null)) continue;
    out.push({ name, from: show(key, before[key]), to: show(key, after[key]) });
  }
  return out;
}

/** 조건을 한 줄로: "총인구 · 부산 · 2021~2025 · 추이". 첫 질문에 무엇으로 읽었는지 보여 준다 */
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

/** 통계표 찾기의 조건(slots.conditions)에서 값이 있는 칸만 */
export function filled(conditions: unknown): [string, string][] {
  if (!isRecord(conditions)) return [];
  return Object.entries(conditions)
    .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => [k, show("", v)]);
}
