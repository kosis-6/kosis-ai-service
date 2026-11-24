import type { Chart, ChartRow } from "../api/types";

const full = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 3 });
const compact = new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 });

export function formatValue(value: number | null | undefined): string {
  return value == null ? "–" : full.format(value);
}

export function axisTick(value: number, axisMax: number): string {
  return Math.abs(axisMax) >= 10_000 ? compact.format(value) : full.format(value);
}

export function shortUnit(unit: string | null | undefined): string {
  return unit && unit.length <= 2 ? unit : "";
}

export function withUnit(value: number | null | undefined, unit: string | null | undefined): string {
  return value == null ? "–" : `${full.format(value)}${shortUnit(unit)}`;
}

export function rowLabel(chart: Pick<Chart, "category">, row: Pick<ChartRow, "label">): string {
  return chart.category === "연도" ? `${row.label}년` : row.label;
}

export function labelWidth(rows: Pick<ChartRow, "label">[]): number {
  const longest = Math.max(0, ...rows.map((r) => r.label.length));
  return Math.min(168, Math.max(48, longest * 13 + 8));
}

export function maxAbs(rows: Pick<ChartRow, "value">[]): number {
  return Math.max(0, ...rows.map((r) => Math.abs(r.value ?? 0)));
}

export const PART_COLORS = [
  { fill: "#2a78d6", text: "#ffffff" },
  { fill: "#eb6834", text: "#ffffff" },
  { fill: "#1baf7a", text: "#ffffff" },
  { fill: "#eda100", text: "#0b0b0b" },
  { fill: "#e87ba4", text: "#ffffff" },
  { fill: "#008300", text: "#ffffff" },
];

export const LINE_COLORS = PART_COLORS.map((c) => c.fill);

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

export function seriesMax(chart: Pick<Chart, "rows">): number {
  return Math.max(0, ...chart.rows.flatMap((r) => (r.values ?? []).map((v) => Math.abs(v ?? 0))));
}

export function fitsLabel(share: number): boolean {
  return share >= 9;
}

export const MAX_OVERLAP = LINE_COLORS.length;

export type Panel = { name: string; points: { year: number; value: number | null }[]; change: number | null };

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

export function changeText(change: number | null): string {
  if (change == null) return "";
  return `${change > 0 ? "+" : ""}${full.format(change)}%`;
}
