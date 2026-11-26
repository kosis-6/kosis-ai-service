import type { Applied, AskResponse, Chart, Source } from "../api/types";
import { LINE_COLORS } from "./chartFormat";
import { safeUrl } from "./format";
import type { Figure, Report, ReportLine } from "./reports";
import reportCss from "./report.css?raw";
import reportCssUrl from "./report.css?url&no-inline";
import reportViewUrl from "./report-view.js?url&no-inline";

type Row = Record<string, unknown>;
type Table = { title: string; headings: string[]; rows: unknown[][] };
const escape = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!
));
const record = (v: unknown): Row => v && typeof v === "object" && !Array.isArray(v) ? v as Row : {};
const list = (v: unknown): Row[] => Array.isArray(v) ? v.map(record) : [];
const number = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const value = (v: unknown) => v == null || (typeof v === "number" && !Number.isFinite(v))
  ? "자료 없음" : typeof v === "number" ? v.toLocaleString("ko-KR", { maximumFractionDigits: 20 }) : String(v);

const STATUS_HEADING = "자료 상태";
const label = (v: unknown) => v == null ? v : String(v);

export function reportTables(input: unknown): Table[] {
  const r = record(input);
  if (Array.isArray(r.split)) return r.split.flatMap(reportTables);
  const title = [r.region, r.indicator, r.sex, r.year ? `${r.year}년` : null, r.unit].filter(Boolean).join(" · ");
  if (r.status !== "ok") return [{ title, headings: [STATUS_HEADING], rows: [[r.reason ?? "자료 없음"]] }];
  if (list(r.series).length) return [{ title, headings: ["연도", "값"], rows: list(r.series).map((s) => [label(s.year), s.value]) }];
  if (list(r.values).length) return [{ title, headings: ["지역", "값"], rows: list(r.values).map((s) => [s.region, s.value]) }];
  if (list(r.ranking).length) return [{ title, headings: ["순위", "지역", "값"], rows: list(r.ranking).map((s) => [label(s.rank), s.region, s.value]) }];
  if (list(r.parts).length) return [{
    title, headings: ["항목", `원래 값 (${r.count_unit ?? "명"})`, "비중 (%)"],
    rows: list(r.parts).map((s) => [s.label, s.count, s.share]),
  }];
  if (list(r.members).length) return [{
    title, headings: ["지역", "값"],
    rows: [...list(r.members).map((s) => [s.region, s.value]), ["합계", r.value]],
  }];
  if ("value" in r) return [{ title, headings: ["값"], rows: [[r.value]] }];
  return [];
}

export function canReport(data: AskResponse): boolean {
  return data.status === "ok" && !data.result.reused
    && reportTables(data.result).some((t) => t.headings[0] !== STATUS_HEADING);
}

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

function chartSvg(chart: Chart): string {
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

const written = (at: Date) => `<p>작성 시각: ${escape(at.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }))} (한국 시간)</p>`;

function sourceList(sources: Source[]): string {
  return sources.map((s) => {
    const link = s.url && safeUrl(s.url);
    return `<li>${escape(s.name)} · ${escape(s.basis)}${link ? ` · <a href="${escape(link)}" rel="noopener noreferrer">통계표 보기</a>` : ""}</li>`;
  }).join("");
}

function reportBody(data: AskResponse, at: Date): string {
  if (!canReport(data)) throw new Error("수치를 계산한 답변에서 보고서를 저장해 주세요.");
  const title = titleOf(data);
  const conditions = (data.applied ?? []).map((c) => `<li>${escape(c.name)}: ${escape(c.value)}${c.source === "inherited" ? " (앞 질문에서 이어받음)" : c.source === "default" ? " (기본값)" : ""}</li>`).join("");
  const tables = reportTables(data.result).map((t) => `<section><h3>${escape(t.title)}</h3><table><thead><tr>${t.headings.map((h) => `<th>${escape(h)}</th>`).join("")}</tr></thead><tbody>${t.rows.map((r) => `<tr>${r.map((v) => `<td>${escape(value(v))}</td>`).join("")}</tr>`).join("")}</tbody></table></section>`).join("");
  return `${MASTHEAD}<h1>${escape(title)}</h1>${written(at)}<h2>분석 조건</h2><ul>${conditions}</ul>${data.notice ? `<p>${escape(data.notice)}</p>` : ""}<h2>요약과 계산 근거</h2><pre>${escape(data.answer)}</pre><h2>전체 수치</h2>${tables}${data.chart ? `<h2>시각화</h2>${chartSvg(data.chart)}` : ""}<h2>출처</h2><ul>${sourceList(data.sources)}</ul><footer>선택한 답변의 계산 결과로 작성했습니다. 보고서 저장 과정에서 자료를 다시 조회하거나 새로운 숫자를 생성하지 않았습니다. 자료가 없는 칸은 ‘자료 없음’으로 표시합니다.</footer>`;
}

/** 대화창 [보고서]: 이 대화에서 고른 답들로 서버가 만든 보고서 (요약 · 분석 · 담은 결과 · 유의할 점) */
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

const titleOf = (data: AskResponse) => data.question || "통계 분석 보고서";

function page(title: string, head: string, body: string): string {
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escape(title)}</title>${head}</head><body>${body}</body></html>`;
}

export function reportFileName(at = new Date()): string {
  return `통계보고서-${at.toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" })}.html`;
}

function fileOf(title: string, body: string): string {
  const toolbar = `<div class="report-toolbar"><button type="button" class="primary" onclick="window.print()">인쇄 · PDF 저장</button></div>`;
  return page(title, `<style>${reportCss}</style>`, toolbar + body);
}

function previewOf(title: string, body: string, assets: { css: string; script: string }, at: Date): string {
  const file = JSON.stringify(fileOf(title, body)).replace(/</g, "\\u003c");
  const toolbar = `<div class="report-toolbar"><button type="button" class="primary" id="report-save">HTML 파일로 저장</button><button type="button" id="report-print">인쇄 · PDF 저장</button><p>내용을 확인한 뒤 저장하세요. 저장한 파일은 인터넷 없이도 열립니다.</p></div>`;
  return page(
    title,
    `<link rel="stylesheet" href="${escape(assets.css)}">`,
    toolbar + body
      + `<script type="application/json" id="report-file" data-filename="${escape(reportFileName(at))}">${file}</script>`
      + `<script src="${escape(assets.script)}"></script>`,
  );
}

export function buildReport(data: AskResponse, at = new Date()): string {
  return fileOf(titleOf(data), reportBody(data, at));
}

export function buildPreview(data: AskResponse, assets: { css: string; script: string }, at = new Date()): string {
  return previewOf(titleOf(data), reportBody(data, at), assets, at);
}

export function buildBundleReport(report: Report, at = new Date()): string {
  return fileOf(report.title, bundleBody(report, at));
}

function saveFile(html: string, name: string): void {
  const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 미리보기를 새 탭으로 연다. 브라우저가 새 탭을 막으면 파일로 바로 저장한다 */
function openHtml(title: string, body: string, at: Date): "opened" | "saved" {
  const assets = {
    css: new URL(reportCssUrl, window.location.href).href,
    script: new URL(reportViewUrl, window.location.href).href,
  };
  const url = URL.createObjectURL(new Blob([previewOf(title, body, assets, at)], { type: "text/html;charset=utf-8" }));
  if (window.open(url, "_blank")) return "opened";
  URL.revokeObjectURL(url);
  saveFile(fileOf(title, body), reportFileName(at));
  return "saved";
}

export function openReport(data: AskResponse): "opened" | "saved" {
  const at = new Date();
  return openHtml(titleOf(data), reportBody(data, at), at);
}

export function openBundleReport(report: Report): "opened" | "saved" {
  const at = new Date();
  return openHtml(report.title, bundleBody(report, at), at);
}
