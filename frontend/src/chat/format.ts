import type { Axis, Family, Parsed, Recommendation, SearchResponse } from "../api/types";

export const AXIS_LABELS: Record<string, string> = { target: "대상", sex: "성별", edition: "연도" };
const CHECK_LABELS: Record<string, string> = {
  year: "연도",
  region_level: "지역 수준",
  sex: "성별",
  target: "대상",
  projection: "장래 추계",
  spouse: "배우자",
  exclude: "제외 조건",
};
export const NO_RESULT_TITLES: Record<string, string> = {
  out_of_scope: "제공 범위 밖의 질문",
  not_statistics: "통계 질문 안내",
  parse_error: "다시 질문해 주세요",
};
const CARD_LINES = ["추천 통계표:", "조건 확인:", "확인 필요:", "같은 표의 다른 버전:", "바로가기:", "함께 볼 통계표:"];

export function checkLabel(key: string, parsed?: Parsed): string {
  const c = parsed?.conditions ?? {};
  if (key === "year" && c.year) return c.year_end ? `${c.year}~${c.year_end}년` : `${c.year}년`;
  if (key === "region_level" && c.region_level) return c.region_level;
  if (key.includes(":")) return key.split(":")[1];
  return CHECK_LABELS[key] || key;
}

export function checkPills(r: Recommendation, parsed?: Parsed): [string, boolean][] {
  return Object.entries(r.checks ?? {}).map(([k, ok]) => [`${checkLabel(k, parsed)}  ${ok ? "✓" : "✕"}`, ok]);
}

export function metaLine(r: Recommendation, gap = "  ·  "): string {
  return [r.org_nm, r.survey_nm, `${r.period}년`].join(gap);
}

export function pathText(r: Recommendation): string | null {
  if (!r.path) return null;
  const parts = r.path.split(" > ");
  if (parts[parts.length - 1] === r.tbl_nm) parts.pop();
  return parts.join("  ›  ");
}

export function defaultOption(a: Axis): string | undefined {
  if (a.axis === "edition") return a.options[0];
  return a.options.find((o) => o === "전체" || o === "계");
}

export function otherVersions(fam: Family | undefined): string | null {
  const versions = [
    ...(fam?.members ?? []).map((m) => m.label),
    ...(fam?.editions ?? []).map((e) => `${e.period}년판`),
  ];
  if (!versions.length) return null;
  const shown = versions.slice(0, 10).join(", ");
  const rest = versions.length > 10 ? ` 외 ${versions.length - 10}개` : "";
  return `같은 표의 다른 버전: ${shown}${rest}`;
}

export function bubbleText(data: SearchResponse): string {
  const lines = data.answer.split("\n");
  if (data.clarify) return lines[0];
  if (!data.recommendations?.length) return data.answer;
  const kept = lines.filter((l) => {
    if (l.startsWith("함께 볼 통계표:") && !data.together?.length) return true;
    return !CARD_LINES.some((p) => l.startsWith(p));
  }).map((l) => l.replace(/^참고: /, ""));
  if (kept.length === 1) kept.push("아래 통계표에서 자세한 자료를 살펴보세요.");
  return kept.join("\n");
}

export function noticeLines(data: SearchResponse): string {
  const lines = (data.notice ?? "").split("; ");
  if (data.status === "partial_match") lines.push("표의 제공 기간과 조건을 확인해 주세요.");
  return lines.join("\n");
}

export function splitLinks(text: string): { text: string; href?: string }[] {
  const parts: { text: string; href?: string }[] = [];
  let last = 0;
  for (const m of text.matchAll(/https?:\/\/[^\s)]+/g)) {
    if (m.index > last) parts.push({ text: text.slice(last, m.index) });
    parts.push({ text: m[0], href: m[0] });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last) });
  return parts;
}

export function safeUrl(url: string): string | undefined {
  return /^https?:\/\//i.test(url) ? url : undefined;
}

export function memberAnswer(picked: Record<string, string>): { text: string; selections: Record<string, string> } {
  const parts: string[] = [];
  const selections: Record<string, string> = {};
  for (const axis of ["target", "sex"]) {
    if (picked[axis]) {
      parts.push(picked[axis]);
      selections[axis] = picked[axis];
    }
  }
  if (picked.edition) parts.push(`${picked.edition}년판`);
  return { text: parts.join(", "), selections };
}
