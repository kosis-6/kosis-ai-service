import type { Applied, Chart, Source } from "../api/types";
import { LINE_COLORS } from "./chartFormat";
import { safeUrl } from "./format";
import type { Figure, Report, ReportLine } from "./reports";
import reportCssUrl from "./report.css?url&no-inline";

const escape = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!
));
const number = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const value = (v: unknown) => v == null || (typeof v === "number" && !Number.isFinite(v))
  ? "자료 없음" : typeof v === "number" ? v.toLocaleString("ko-KR", { maximumFractionDigits: 20 }) : String(v);

// 화면 차트(Chart.tsx)와 같은 색: 선 · 막대 하나는 SERIES, 여러 선 · 구성비 조각은 LINE_COLORS
const SERIES = "#245941";
const colors = LINE_COLORS;

function gridSvg(chart: Chart): string {
  const cells = (chart.series ?? []).map((name, j) => {
    const pts = chart.rows
      .map((r, i) => ({ i, v: r.values?.[j] }))
      .filter((p): p is { i: number; v: number } => number(p.v));
    if (!pts.length) return "";
    const lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v)), span = hi - lo || 1;
    const x = (i: number) => 8 + (i / Math.max(1, chart.rows.length - 1)) * 204;
    const y = (v: number) => 70 - ((v - lo) / span) * 56;
    const line = pts.map((p) => `${x(p.i)},${y(p.v)}`).join(" ");
    const lastPt = pts[pts.length - 1];
    return `<div class="report-cell"><h4>${escape(name)}</h4><p>${escape(`${chart.rows[lastPt.i].label}년 ${value(lastPt.v)} ${chart.unit}`)}</p><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 78" role="img" aria-label="${escape(name)}"><polyline points="${line}" fill="none" stroke="#245941" stroke-width="2"/></svg></div>`;
  }).join("");
  return `<figure><h3>${escape(chart.title)}</h3><p>단위: ${escape(chart.unit)} · 지역이 많아 지역마다 따로 그렸습니다. 세로축은 지역마다 다릅니다</p><div class="report-grid">${cells}</div></figure>`;
}

export function chartSvg(chart: Chart): string {
  const rows = chart.rows;
  if (!rows.length || chart.view === "table") return "";
  if (chart.type === "line" && (chart.crowded ?? (chart.series?.length ?? 0) > colors.length)) return gridSvg(chart);
  const width = 780, left = 150, right = 730;
  let marks = "";
  let height = 300;
  if (chart.type === "line") {
    const series = chart.series?.length ? chart.series : [chart.title];
    const get = (i: number, j: number) => chart.series?.length ? rows[i].values?.[j] : rows[i].value;
    const all = rows.flatMap((_, i) => series.map((_, j) => get(i, j))).filter(number);
    if (!all.length) return "";
    const lo = Math.min(0, ...all), hi = Math.max(0, ...all), span = hi - lo || 1;
    const years = rows.map((r) => Number(r.label));
    const numeric = years.every(Number.isFinite) && Math.max(...years) > Math.min(...years);
    const x = (i: number) => numeric
      ? left + (years[i] - Math.min(...years)) / (Math.max(...years) - Math.min(...years)) * (right - left)
      : left + i / Math.max(1, rows.length - 1) * (right - left);
    const y = (n: number) => 220 - (n - lo) / span * 170;
    const bar = chart.view === "bar";
    const spacing = Math.min(...rows.slice(1).map((_, i) => Math.abs(x(i + 1) - x(i))).filter(n => n > 0), 60);
    const barWidth = spacing * .8 / series.length;
    marks += `<text x="10" y="45">${escape(value(hi))}</text><text x="10" y="225">${escape(value(lo))}</text>`;
    const tone = (j: number) => chart.series?.length ? colors[j % colors.length] : SERIES;
    series.forEach((name, j) => {
      let segment: string[] = [];
      const flush = () => { if (segment.length) marks += `<polyline points="${segment.join(" ")}" fill="none" stroke="${tone(j)}" stroke-width="2"/>`; segment = []; };
      rows.forEach((r, i) => {
        const n = get(i, j);
        if (!number(n)) { flush(); return; }
        const title = escape(`${name} ${r.label}: ${value(n)} ${chart.unit}`);
        if (bar) {
          const bx = x(i) + (j - series.length / 2) * barWidth;
          marks += `<rect x="${bx}" y="${Math.min(y(0), y(n))}" width="${barWidth}" height="${Math.abs(y(0) - y(n))}" fill="${tone(j)}"><title>${title}</title></rect>`;
          return;
        }
        segment.push(`${x(i)},${y(n)}`);
        marks += `<circle cx="${x(i)}" cy="${y(n)}" r="3" fill="${tone(j)}"><title>${title}</title></circle>`;
      });
      flush();
      marks += `<text x="${left + (j % 3) * 190}" y="${265 + Math.floor(j / 3) * 20}" fill="${tone(j)}">${escape(name)}</text>`;
    });
    rows.forEach((r, i) => {
      if (i === 0 || i === rows.length - 1 || rows.length <= 10) marks += `<text x="${x(i)}" y="242" text-anchor="middle">${escape(r.label)}</text>`;
    });
  } else {
    height = Math.max(140, rows.length * 32 + 65);
    const all = rows.map((r) => r.value).filter(number);
    const lo = Math.min(0, ...all), hi = Math.max(0, ...all), span = hi - lo || 1;
    const x = (n: number) => left + (n - lo) / span * (right - left - 80);
    marks += `<line x1="${x(0)}" y1="15" x2="${x(0)}" y2="${height - 35}" stroke="#777"/>`;
    rows.forEach((r, i) => {
      const n = r.value, y = 25 + i * 32;
      marks += `<text x="140" y="${y + 15}" text-anchor="end">${escape(r.label)}</text>`;
      if (number(n)) marks += `<rect x="${Math.min(x(0), x(n))}" y="${y}" width="${Math.abs(x(n) - x(0))}" height="20" fill="${chart.type === "parts" ? colors[i % colors.length] : SERIES}"/><text x="${Math.max(x(n), x(0)) + 8}" y="${y + 15}">${escape(value(n))}</text>`;
    });
  }
  return `<figure><h3>${escape(chart.title)}</h3><p>단위: ${escape(chart.unit)}${chart.type === "parts" ? " · 보고서에서는 항목별 막대로 표시" : ""}</p><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escape(chart.title)}" font-family="sans-serif" font-size="12">${marks}</svg>${chart.note ? `<p>${escape(chart.note)}</p>` : ""}</figure>`;
}

function sourceList(sources: Source[]): string {
  return sources.map((s) => {
    const link = s.url && safeUrl(s.url);
    return `<li>${escape(s.name)} · ${escape(s.basis)}${link ? ` · <a href="${escape(link)}" rel="noopener noreferrer">통계표 보기</a>` : ""}</li>`;
  }).join("");
}

// 문서 머리글: 어느 서비스가 만든 보고서인지 (인쇄 · PDF에도 남는다)
const MASTHEAD = `<header class="masthead"><p><strong>LIKE SNAKE</strong>인구 통계 안내</p><p>KOSIS 국가통계포털 자료 기준</p></header>`;

const withUnit = (v: number | null | undefined, unit = "") => number(v) ? `${value(v)}${unit}` : "자료 없음";

/** 핵심 수치 줄에 쓸 한 값: 값이 하나면 그 값, 추이는 마지막 해, 순위는 1위, 구성비는 가장 큰 조각 */
function headline(fig: Figure | null | undefined, valueText: string | null | undefined): string {
  const rows = fig?.rows ?? [];
  const unit = fig?.unit ?? "";
  // 추이는 어느 해 값인지 함께 ('2021~2025년 …'의 값이 14,010명이면 몇 년 값인지 알 수 없었다)
  if (fig?.kind === "series" && rows.length) return `${rows[rows.length - 1].label} ${withUnit(rows[rows.length - 1].value, unit)}`;
  if (valueText) return valueText;
  if (!rows.length) return withUnit(fig?.value, unit);
  if (fig?.kind === "ranking") return `1위 ${rows[0].label} ${withUnit(rows[0].value, unit)}`;
  if (fig?.kind === "parts") {
    const top = rows.reduce((a, b) => ((b.value ?? -Infinity) > (a.value ?? -Infinity) ? b : a));
    return `${top.label} ${withUnit(top.value, "%")}`;
  }
  return `${rows.length}개 지역`;
}

/** 결과 하나의 수치 표: 추이 · 비교 · 순위 · 구성비는 전체 행, 비율은 분자 · 분모, 값 하나는 그 값 */
function figureTable(fig: Figure | null | undefined, valueText: string | null | undefined): string {
  if (!fig) return "";
  const unit = fig.unit ? ` (${fig.unit})` : "";
  const rows = fig.rows ?? [];
  let head: string[];
  let body: unknown[][];
  if (rows.length && fig.kind === "series") [head, body] = [["연도", `값${unit}`], rows.map((r) => [r.label, r.value])];
  else if (rows.length && fig.kind === "ranking") [head, body] = [["순위", "지역", `값${unit}`], rows.map((r) => [r.rank, r.label, r.value])];
  else if (rows.length && fig.kind === "parts") [head, body] = [["항목", "비중 (%)", "원래 값"], rows.map((r) => [r.label, r.value, r.count])];
  else if (rows.length) [head, body] = [["지역", `값${unit}`], rows.map((r) => [r.label, r.value])];
  else if (fig.operands?.length) {
    head = ["항목", "값"];
    body = [...fig.operands.map((o) => [o.label, withUnit(o.value, o.unit)]), [fig.label ?? "결과", valueText ?? withUnit(fig.value, fig.unit)]];
  } else [head, body] = [["항목", "값"], [[fig.label ?? "", valueText ?? withUnit(fig.value, fig.unit)]]];
  // 숫자 열은 오른쪽 정렬: 첫 열(연도 · 지역 · 항목 · 순위)과 순위표의 지역 열만 글자
  const numeric = (i: number) => i > 0 && (fig.kind !== "ranking" || i === head.length - 1);
  const td = (v: unknown, i: number) => `<td${numeric(i) ? ' class="num"' : ""}>${escape(value(v))}</td>`;
  const th = head.map((h, i) => `<th${numeric(i) ? ' class="num"' : ""}>${escape(h)}</th>`).join("");
  const tr = body.map((r) => `<tr>${r.map(td).join("")}</tr>`).join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}

const SOURCE_NOTE: Record<string, string> = { inherited: "앞 질문에서 이어받음", default: "기본값" };

function conditionList(applied: Applied[]): string {
  const items = applied.map((c) => `<li><span>${escape(c.name)}</span>${escape(c.value)}${SOURCE_NOTE[c.source] ? `<em>${SOURCE_NOTE[c.source]}</em>` : ""}</li>`).join("");
  return items ? `<ul class="conditions" aria-label="적용한 조건">${items}</ul>` : "";
}

/** 대화창 [보고서]: 이 대화에서 고른 답들로 서버가 만든 보고서.
 *  머리글 → 제목 → 핵심 수치 → 요약 → 분석(있을 때) → 결과별 내용(조건 · 표 · 차트 · 출처) → 유의할 점 */
export function bundleBody(report: Report, at: Date): string {
  const c = report.content ?? {};
  const items = c.items ?? [];
  const snaps = new Map(report.items.map((i) => [i.turn_id, i.snapshot ?? {}]));
  const lines = (xs: ReportLine[] = []) => xs.map((l) => `<li>${escape(l.text)}</li>`).join("");
  const figures = items.map((it) => {
    const fig = it.figure ?? snaps.get(it.turn_id)?.figure;
    return `<div><dt>${escape(fig?.label || it.label || it.question || "")}</dt><dd>${escape(headline(fig, it.value_text))}</dd></div>`;
  }).join("");
  const results = items.map((it) => {
    const fig = it.figure ?? snaps.get(it.turn_id)?.figure;
    return `<section class="result"><h3>${escape(it.question || it.label || "")}</h3>${conditionList(snaps.get(it.turn_id)?.applied ?? [])}${figureTable(fig, it.value_text)}${it.chart ? chartSvg(it.chart) : ""}${it.sources?.length ? `<ul class="sources">${sourceList(it.sources)}</ul>` : ""}</section>`;
  }).join("");
  const notes = [...(c.limits ?? []), ...(c.warnings ?? [])].map((n) => `<li>${escape(n)}</li>`).join("");
  const when = at.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", dateStyle: "long", timeStyle: "short" });
  return `${MASTHEAD}<h1>${escape(report.title)}</h1><p class="meta">결과 ${items.length}개 · ${escape(when)} 작성 (한국 시간)</p>${report.purpose ? `<p class="purpose">${escape(report.purpose)}</p>` : ""}${figures ? `<dl class="figures">${figures}</dl>` : ""}<h2>요약</h2><ul class="lines">${lines(c.summary)}</ul>${c.analysis?.length ? `<h2>분석</h2><ul class="lines">${lines(c.analysis)}</ul>` : ""}<h2>결과별 내용</h2>${results}${notes ? `<h2>유의할 점</h2><ul class="notes">${notes}</ul>` : ""}<footer>대화에서 고른 답의 계산 결과로 작성했습니다. 문장 속 숫자는 계산 결과를 그대로 넣었고, 보고서를 만들며 자료를 다시 조회하거나 새로운 숫자를 생성하지 않았습니다.</footer>`;
}

// 보고서 글꼴: 서비스와 같은 Noto Sans KR (제목 굵기 700까지)
const FONTS = "https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700&display=swap";

/** 인쇄 창의 'PDF로 저장' 파일 이름이 되는 문서 제목 */
export function pdfName(report: Report, at = new Date()): string {
  return `${report.title} ${at.toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" })}`;
}

/** 앱 안 미리보기(iframe srcdoc)이자 인쇄 · PDF로 저장하는 문서.
 *  페이지 보안 정책(CSP)이 문서 안 스타일 · 스크립트를 막아 스타일은 파일로 불러오고, 스크립트는 두지 않는다.
 *  출처 링크는 미리보기 안이 아니라 새 탭에서 연다 */
export function reportDocument(report: Report, at = new Date(), cssUrl: string = reportCssUrl): string {
  const head = `<base target="_blank"><link rel="stylesheet" href="${escape(FONTS)}"><link rel="stylesheet" href="${escape(cssUrl)}">`;
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escape(pdfName(report, at))}</title>${head}</head><body>${bundleBody(report, at)}</body></html>`;
}
