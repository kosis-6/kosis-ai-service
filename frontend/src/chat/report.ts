/** 표시한 답변으로 독립 HTML 보고서를 만든다. 서버 재조회·LLM 호출·영구 저장은 하지 않는다. */
import type { AskResponse, Chart } from "../api/types";
import { safeUrl } from "./format";
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
// 연도·순위 같은 이름 칸은 숫자여도 쉼표 없이 쓴다 (2,024가 아니라 2024)
const label = (v: unknown) => v == null ? v : String(v);

/** 차트의 15개 표시 제한과 관계없이 실제 계산 결과의 전체 행을 사용한다. */
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
  // 수치 표가 하나도 없으면(빈 결과·실패한 결과만 있음) 빈 보고서가 되므로 만들지 않는다
  return data.status === "ok" && !data.result.reused
    && reportTables(data.result).some((t) => t.headings[0] !== STATUS_HEADING);
}

const colors = ["#276749", "#2b6cb0", "#a65c00", "#805ad5", "#b83280", "#157a86"];

/** HTML 파일만 열어도 보이도록 외부 스크립트·차트 라이브러리 없이 SVG를 넣는다. */
function chartSvg(chart: Chart): string {
  const rows = chart.rows;
  if (!rows.length) return "";
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
    marks += `<text x="10" y="45">${escape(value(hi))}</text><text x="10" y="225">${escape(value(lo))}</text>`;
    series.forEach((name, j) => {
      let segment: string[] = [];
      const flush = () => { if (segment.length) marks += `<polyline points="${segment.join(" ")}" fill="none" stroke="${colors[j % colors.length]}" stroke-width="2"/>`; segment = []; };
      rows.forEach((r, i) => {
        const n = get(i, j);
        if (!number(n)) { flush(); return; }
        segment.push(`${x(i)},${y(n)}`);
        marks += `<circle cx="${x(i)}" cy="${y(n)}" r="3" fill="${colors[j % colors.length]}"><title>${escape(`${name} ${r.label}: ${value(n)} ${chart.unit}`)}</title></circle>`;
      });
      flush();
      marks += `<text x="${left + (j % 3) * 190}" y="${265 + Math.floor(j / 3) * 20}" fill="${colors[j % colors.length]}">${escape(name)}</text>`;
    });
    rows.forEach((r, i) => {
      if (i === 0 || i === rows.length - 1 || rows.length <= 10) marks += `<text x="${x(i)}" y="242" text-anchor="middle">${escape(r.label)}</text>`;
    });
  } else {
    // 구성비도 항목별 수평 막대로 표현하며 보고서에 표시 방식을 명시한다.
    height = Math.max(140, rows.length * 32 + 65);
    const all = rows.map((r) => r.value).filter(number);
    const lo = Math.min(0, ...all), hi = Math.max(0, ...all), span = hi - lo || 1;
    const x = (n: number) => left + (n - lo) / span * (right - left - 80);
    marks += `<line x1="${x(0)}" y1="15" x2="${x(0)}" y2="${height - 35}" stroke="#777"/>`;
    rows.forEach((r, i) => {
      const n = r.value, y = 25 + i * 32;
      marks += `<text x="140" y="${y + 15}" text-anchor="end">${escape(r.label)}</text>`;
      if (number(n)) marks += `<rect x="${Math.min(x(0), x(n))}" y="${y}" width="${Math.abs(x(n) - x(0))}" height="20" fill="${colors[i % colors.length]}"/><text x="${Math.max(x(n), x(0)) + 8}" y="${y + 15}">${escape(value(n))}</text>`;
    });
  }
  return `<figure><h3>${escape(chart.title)}</h3><p>단위: ${escape(chart.unit)}${chart.type === "parts" ? " · 보고서에서는 항목별 막대로 표시" : ""}</p><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escape(chart.title)}" font-family="sans-serif" font-size="12">${marks}</svg>${chart.note ? `<p>${escape(chart.note)}</p>` : ""}</figure>`;
}

/** 보고서 본문 (저장 파일·미리보기 공통) */
function reportBody(data: AskResponse, at: Date): string {
  if (!canReport(data)) throw new Error("수치를 계산한 답변에서 보고서를 저장해 주세요.");
  const title = data.question || "통계 분석 보고서";
  const conditions = (data.applied ?? []).map((c) => `<li>${escape(c.name)}: ${escape(c.value)}${c.source === "inherited" ? " (앞 질문에서 이어받음)" : c.source === "default" ? " (기본값)" : ""}</li>`).join("");
  const tables = reportTables(data.result).map((t) => `<section><h3>${escape(t.title)}</h3><table><thead><tr>${t.headings.map((h) => `<th>${escape(h)}</th>`).join("")}</tr></thead><tbody>${t.rows.map((r) => `<tr>${r.map((v) => `<td>${escape(value(v))}</td>`).join("")}</tr>`).join("")}</tbody></table></section>`).join("");
  const sources = data.sources.map((s) => {
    const link = s.url && safeUrl(s.url);
    return `<li>${escape(s.name)} · ${escape(s.basis)}${link ? ` · <a href="${escape(link)}" rel="noopener noreferrer">통계표 보기</a>` : ""}</li>`;
  }).join("");
  return `<h1>${escape(title)}</h1><p>작성 시각: ${escape(at.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }))} (한국 시간)</p><h2>분석 조건</h2><ul>${conditions}</ul>${data.notice ? `<p>${escape(data.notice)}</p>` : ""}<h2>요약과 계산 근거</h2><pre>${escape(data.answer)}</pre><h2>전체 수치</h2>${tables}${data.chart ? `<h2>시각화</h2>${chartSvg(data.chart)}` : ""}<h2>출처</h2><ul>${sources}</ul><footer>선택한 답변의 계산 결과로 작성했습니다. 보고서 저장 과정에서 자료를 다시 조회하거나 새로운 숫자를 생성하지 않았습니다. 자료가 없는 칸은 ‘자료 없음’으로 표시합니다.</footer>`;
}

function page(data: AskResponse, head: string, body: string): string {
  const title = escape(data.question || "통계 분석 보고서");
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title>${head}</head><body>${body}</body></html>`;
}

export function reportFileName(at = new Date()): string {
  return `통계보고서-${at.toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" })}.html`;
}

/** 저장하는 파일: 스타일을 안에 담아 파일만 열어도 보인다 (파일로 열면 CSP가 없어 인쇄 버튼도 동작) */
export function buildReport(data: AskResponse, at = new Date()): string {
  const toolbar = `<div class="report-toolbar"><button type="button" class="primary" onclick="window.print()">인쇄 · PDF 저장</button></div>`;
  return page(data, `<style>${reportCss}</style>`, toolbar + reportBody(data, at));
}

/** 새 탭 미리보기: 채팅 화면의 CSP를 물려받아 인라인 스타일·스크립트가 막히므로
 *  스타일과 버튼 동작은 우리 주소의 파일로 불러온다. 저장할 파일 내용은 JSON으로 함께 넣는다 */
export function buildPreview(data: AskResponse, assets: { css: string; script: string }, at = new Date()): string {
  const file = JSON.stringify(buildReport(data, at)).replace(/</g, "\\u003c");
  const toolbar = `<div class="report-toolbar"><button type="button" class="primary" id="report-save">HTML 파일로 저장</button><button type="button" id="report-print">인쇄 · PDF 저장</button><p>내용을 확인한 뒤 저장하세요. 저장한 파일은 인터넷 없이도 열립니다.</p></div>`;
  return page(
    data,
    `<link rel="stylesheet" href="${escape(assets.css)}">`,
    toolbar + reportBody(data, at)
      + `<script type="application/json" id="report-file" data-filename="${escape(reportFileName(at))}">${file}</script>`
      + `<script src="${escape(assets.script)}"></script>`,
  );
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

/** 보고서를 새 탭에 띄운다. 브라우저가 새 탭을 막으면 파일로 바로 저장하고 "saved"를 돌려준다 */
export function openReport(data: AskResponse): "opened" | "saved" {
  const at = new Date();
  const assets = {
    css: new URL(reportCssUrl, window.location.href).href,
    script: new URL(reportViewUrl, window.location.href).href,
  };
  const html = buildPreview(data, assets, at);
  // 탭을 새로 고쳐도 보이도록 주소는 바로 지우지 않는다 (채팅 화면을 닫으면 함께 사라진다)
  const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
  if (window.open(url, "_blank")) return "opened";
  URL.revokeObjectURL(url);
  saveFile(buildReport(data, at), reportFileName(at));
  return "saved";
}
