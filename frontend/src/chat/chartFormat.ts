// 차트에 쓰는 숫자·글자 규칙 (그리기와 떼어 둔 순수 함수)
import type { Chart, ChartRow } from "../api/types";

const full = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 3 });
const compact = new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 });

/** 값 그대로 (자릿수 쉼표). 자료가 없으면 "–" */
export function formatValue(value: number | null | undefined): string {
  return value == null ? "–" : full.format(value);
}

/** 축 눈금: 만 이상이 있는 축은 줄여 쓴다 (2만, 15.3만). 한 축에는 한 가지 방식만 */
export function axisTick(value: number, axisMax: number): string {
  return Math.abs(axisMax) >= 10_000 ? compact.format(value) : full.format(value);
}

/** 값 옆에 바로 붙일 수 있는 짧은 단위(%, 명, 건). 긴 단위("가임여성 1명당 명")는 제목 아래에만 쓴다 */
export function shortUnit(unit: string | null | undefined): string {
  return unit && unit.length <= 2 ? unit : "";
}

export function withUnit(value: number | null | undefined, unit: string | null | undefined): string {
  return value == null ? "–" : `${full.format(value)}${shortUnit(unit)}`;
}

/** 행 이름: 연도는 "2024년" */
export function rowLabel(chart: Pick<Chart, "category">, row: Pick<ChartRow, "label">): string {
  return chart.category === "연도" ? `${row.label}년` : row.label;
}

/** 가로 막대 왼쪽 이름 칸 너비(px): 가장 긴 이름에 맞추되 너무 넓어지지 않게 */
export function labelWidth(rows: Pick<ChartRow, "label">[]): number {
  const longest = Math.max(0, ...rows.map((r) => r.label.length));
  return Math.min(168, Math.max(48, longest * 13 + 8));
}

/** 값이 있는 행들의 가장 큰 절댓값 (축 눈금 방식을 고를 때) */
export function maxAbs(rows: Pick<ChartRow, "value">[]): number {
  return Math.max(0, ...rows.map((r) => Math.abs(r.value ?? 0)));
}

// 구성비 조각 색: 순서대로 쓰고 돌려 쓰지 않는다 (조각은 6개까지, 서버 charts.MAX_PARTS).
// 흰 바탕에서 색각 이상 구분·명도 검사를 통과한 순서다. text 는 조각 안 글자색
export const PART_COLORS = [
  { fill: "#2a78d6", text: "#ffffff" },
  { fill: "#eb6834", text: "#ffffff" },
  { fill: "#1baf7a", text: "#ffffff" },
  { fill: "#eda100", text: "#0b0b0b" },
  { fill: "#e87ba4", text: "#ffffff" },
  { fill: "#008300", text: "#ffffff" },
];

// 여러 꺾은선(지역별 추이)의 선 색: 구성비와 같은 순서·같은 검사를 통과한 색 (서버 charts.MAX_LINES 개까지)
export const LINE_COLORS = PART_COLORS.map((c) => c.fill);

/** 여러 꺾은선의 행 → Recharts 행: { year, s0, s1, ... } (series 순서). 자료가 없는 해는 비운다 */
export function seriesRows(chart: Pick<Chart, "rows" | "series">): Record<string, number | null>[] {
  const names = chart.series ?? [];
  return chart.rows.map((r) => {
    const row: Record<string, number | null> = { year: Number(r.label) };
    names.forEach((_, i) => {
      row[`s${i}`] = r.values?.[i] ?? null;
    });
    return row;
  });
}

/** 여러 꺾은선의 모든 값 중 가장 큰 절댓값 */
export function seriesMax(chart: Pick<Chart, "rows">): number {
  return Math.max(0, ...chart.rows.flatMap((r) => (r.values ?? []).map((v) => Math.abs(v ?? 0))));
}

/** 조각 안에 비중 글자를 넣을 만큼 넓은가 (좁으면 넣지 않고 범례·표가 대신한다) */
export function fitsLabel(share: number): boolean {
  return share >= 9;
}

/** 한 그래프에 겹쳐 그리는 선의 최대 개수 (색을 돌려 쓰지 않는다. 서버 charts.MAX_LINES와 같다).
 *  지역이 이보다 많으면 지역마다 작은 그래프를 나란히 그린다 */
export const MAX_OVERLAP = LINE_COLORS.length;

export type Panel = { name: string; points: { year: number; value: number | null }[]; change: number | null };

/** 여러 꺾은선 → 지역마다 작은 그래프 하나. change: 값이 있는 처음 해 대비 마지막 해 증감률(%) */
export function panels(chart: Pick<Chart, "rows" | "series">): Panel[] {
  return (chart.series ?? []).map((name, i) => {
    const points = chart.rows.map((r) => ({ year: Number(r.label), value: r.values?.[i] ?? null }));
    const known = points.filter((p): p is { year: number; value: number } => p.value != null);
    const first = known[0]?.value;
    const last = known[known.length - 1]?.value;
    const change =
      known.length >= 2 && first ? Math.round(((last! - first) / Math.abs(first)) * 1000) / 10 : null;
    return { name, points, change };
  });
}

/** 증감률 글자: "+2.3%", "-4.4%", 변화 없음 "0%" */
export function changeText(change: number | null): string {
  if (change == null) return "";
  return `${change > 0 ? "+" : ""}${full.format(change)}%`;
}
