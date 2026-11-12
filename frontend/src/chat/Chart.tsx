// 답변의 차트 (POST /ask 의 chart). 서버가 종류와 행을 정하고 여기서는 그리기만 한다
//   line   연도별 꺾은선 (막대로 바꿔 볼 수 있다. 다시 조회하지 않는다)
//   bar    지역별 가로 막대 (큰 순·순위 순)
//   parts  구성비 나뉜 막대
// 모든 차트는 "표"로도 볼 수 있다 (색이나 마우스 없이도 같은 값을 읽을 수 있게)
import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Chart } from "../api/types";
import {
  PART_COLORS,
  axisTick,
  fitsLabel,
  formatValue,
  labelWidth,
  maxAbs,
  rowLabel,
  shortUnit,
  withUnit,
} from "./chartFormat";

// styles/base.css 의 토큰과 같은 값 (SVG 속성에는 CSS 변수를 쓸 수 없는 브라우저가 있다)
const INK = "#173b2e";
const MUTED = "#687c6e";
const GRID = "#e1e9df";
const SERIES = "#245941";
const SURFACE = "#ffffff";
const HOVER = "#e7f1e5";

const TICK = { fontSize: 11, fill: MUTED };

type TipProps = { active?: boolean; payload?: readonly { value?: unknown }[]; label?: unknown };

// 마우스를 올린 위치의 값. 값을 크게, 이름을 작게 (읽는 사람은 이름을 알고 값을 찾는다)
function Tip({ active, payload, label, chart }: TipProps & { chart: Chart }) {
  const value = payload?.[0]?.value;
  if (!active || typeof value !== "number") return null;
  return (
    <div className="chart-tip">
      <strong>{withUnit(value, chart.unit)}</strong>
      <span>{rowLabel(chart, { label: String(label) })}</span>
    </div>
  );
}

function LineView({ chart }: { chart: Chart }) {
  // 가로축은 연도를 숫자로 놓는다: 자료가 5년마다 있다가 매년 있는 지표도 간격이 실제 햇수와 맞는다
  const rows = chart.rows.map((r) => ({ ...r, year: Number(r.label) }));
  const last = rows.length - 1;
  const top = maxAbs(rows);
  return (
    <ResponsiveContainer width="100%" height={248}>
      <LineChart data={rows} margin={{ top: 24, right: 28, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke={GRID} />
        <XAxis
          dataKey="year"
          type="number"
          domain={["dataMin", "dataMax"]}
          // 눈금은 자료가 있는 해에만 찍는다 (겹치면 Recharts 가 일부를 숨긴다)
          ticks={rows.map((r) => r.year)}
          interval="preserveStartEnd"
          allowDecimals={false}
          padding={{ left: 12, right: 12 }}
          tickLine={false}
          axisLine={{ stroke: GRID }}
          tick={TICK}
          minTickGap={20}
        />
        <YAxis
          width={48}
          tickLine={false}
          axisLine={false}
          tick={TICK}
          domain={["auto", "auto"]}
          tickFormatter={(v: number) => axisTick(v, top)}
        />
        <Tooltip cursor={{ stroke: MUTED, strokeWidth: 1 }} content={(p) => <Tip {...p} chart={chart} />} />
        <Line
          dataKey="value"
          stroke={SERIES}
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          // 점이 많으면 선만 그린다. 점에는 바탕색 테두리를 둘러 선과 겹쳐도 보이게 한다
          dot={rows.length <= 24 ? { r: 4, fill: SERIES, stroke: SURFACE, strokeWidth: 2 } : false}
          activeDot={{ r: 6, fill: SERIES, stroke: SURFACE, strokeWidth: 2 }}
          isAnimationActive={false}
        >
          {/* 값은 처음과 끝에만 적는다 (모든 점에 적으면 읽히지 않는다). 나머지는 축·툴팁·표 */}
          <LabelList
            dataKey="value"
            content={({ x, y, index, value }) =>
              (index === 0 || index === last) && typeof value === "number" ? (
                // 첫 값은 점 오른쪽으로 적어 세로축 눈금과 겹치지 않게 한다
                <text
                  x={Number(x) + (index === 0 ? 8 : 0)}
                  y={Number(y) - 12}
                  textAnchor={index === 0 ? "start" : "middle"}
                  fontSize={11}
                  fontWeight={500}
                  fill={INK}
                >
                  {formatValue(value)}
                </text>
              ) : null
            }
          />
        </Line>
      </LineChart>
    </ResponsiveContainer>
  );
}

// 연도별 값을 세로 막대로 (꺾은선에서 바꿔 볼 때). 막대는 0에서 시작해야 길이가 값과 맞는다
function ColumnView({ chart }: { chart: Chart }) {
  const top = maxAbs(chart.rows);
  return (
    <ResponsiveContainer width="100%" height={248}>
      <BarChart data={chart.rows} margin={{ top: 24, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke={GRID} />
        <XAxis dataKey="label" tickLine={false} axisLine={{ stroke: GRID }} tick={TICK} minTickGap={8} />
        <YAxis width={48} tickLine={false} axisLine={false} tick={TICK} tickFormatter={(v: number) => axisTick(v, top)} />
        <Tooltip cursor={{ fill: HOVER }} content={(p) => <Tip {...p} chart={chart} />} />
        <Bar dataKey="value" fill={SERIES} maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function BarView({ chart }: { chart: Chart }) {
  const rows = chart.rows;
  const negative = rows.some((r) => (r.value ?? 0) < 0);
  return (
    <ResponsiveContainer width="100%" height={rows.length * 36 + 16}>
      <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 72, bottom: 4, left: 0 }}>
        <XAxis type="number" hide domain={negative ? ["auto", "auto"] : [0, "auto"]} />
        <YAxis
          type="category"
          dataKey="label"
          width={labelWidth(rows)}
          tickLine={false}
          axisLine={false}
          tick={{ fontSize: 12, fill: INK }}
          interval={0}
        />
        <Tooltip cursor={{ fill: HOVER }} content={(p) => <Tip {...p} chart={chart} />} />
        <Bar dataKey="value" fill={SERIES} maxBarSize={20} radius={[0, 4, 4, 0]} isAnimationActive={false}>
          <LabelList
            dataKey="value"
            position="right"
            fontSize={12}
            fill={INK}
            formatter={(v: unknown) => (typeof v === "number" ? withUnit(v, chart.unit) : "")}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// 전체 하나가 어떻게 나뉘는지: 28px 막대를 비중만큼 나눈다. 이름은 아래 범례가, 값은 조각 안 글자와 표가 전한다
function PartsView({ chart }: { chart: Chart }) {
  const rows = chart.rows;
  const describe = (i: number) => {
    const r = rows[i];
    const count = r.count == null ? "" : ` (${formatValue(r.count)}${chart.count_unit ?? ""})`;
    return `${r.label}: ${withUnit(r.value, chart.unit)}${count}`;
  };
  return (
    <div className="parts">
      <div className="parts-bar" role="img" aria-label={rows.map((_, i) => describe(i)).join(", ")}>
        {rows.map((r, i) =>
          r.value ? (
            <span
              key={r.label}
              className="parts-seg"
              title={describe(i)}
              style={{ flexGrow: r.value, background: PART_COLORS[i].fill, color: PART_COLORS[i].text }}
            >
              {fitsLabel(r.value) ? withUnit(r.value, chart.unit) : ""}
            </span>
          ) : null,
        )}
      </div>
      <ul className="parts-legend">
        {rows.map((r, i) => (
          <li key={r.label}>
            <span className="parts-key" style={{ background: PART_COLORS[i].fill }} />
            {r.label}
          </li>
        ))}
      </ul>
    </div>
  );
}

function TableView({ chart }: { chart: Chart }) {
  const hasCount = chart.rows.some((r) => r.count != null);
  return (
    <table className="chart-table">
      <thead>
        <tr>
          <th scope="col">{chart.category}</th>
          {hasCount && <th scope="col">{`값 (${chart.count_unit ?? ""})`}</th>}
          <th scope="col">{hasCount ? `비중 (${chart.unit})` : `값${shortUnit(chart.unit) ? ` (${chart.unit})` : ""}`}</th>
        </tr>
      </thead>
      <tbody>
        {chart.rows.map((r) => (
          <tr key={r.label}>
            <th scope="row">{rowLabel(chart, r)}</th>
            {hasCount && <td>{formatValue(r.count)}</td>}
            <td>{formatValue(r.value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

type View = "line" | "bar" | "table";

export default function ChartCard({ chart }: { chart: Chart }) {
  // 꺾은선은 막대로도 볼 수 있다. 가로 막대·구성비는 차트 하나와 표
  const views: [View, string][] =
    chart.type === "line"
      ? [
          ["line", "꺾은선"],
          ["bar", "막대"],
          ["table", "표"],
        ]
      : [
          [chart.type === "bar" ? "bar" : "line", "차트"],
          ["table", "표"],
        ];
  // "막대그래프로", "표로"라고 했으면 서버가 정한 모양으로 시작한다 (그릴 수 있는 모양일 때만)
  const asked = views.find(([key]) => key === chart.view)?.[0];
  const [view, setView] = useState<View>(asked ?? views[0][0]);

  return (
    <figure className="chart-card">
      <figcaption className="chart-head">
        <div className="chart-titles">
          <h3>{chart.title}</h3>
          <p className="chart-unit">{`단위: ${chart.unit}`}</p>
        </div>
        <div className="chart-views" role="group" aria-label="보기 방식">
          {views.map(([key, label]) => (
            <button
              type="button"
              key={key}
              className={view === key ? "pill on" : "pill"}
              aria-pressed={view === key}
              onClick={() => setView(key)}
            >
              {label}
            </button>
          ))}
        </div>
      </figcaption>
      {view === "table" ? (
        <TableView chart={chart} />
      ) : chart.type === "parts" ? (
        <PartsView chart={chart} />
      ) : chart.type === "bar" ? (
        <BarView chart={chart} />
      ) : view === "bar" ? (
        <ColumnView chart={chart} />
      ) : (
        <LineView chart={chart} />
      )}
      {chart.note && <p className="chart-note">{chart.note}</p>}
    </figure>
  );
}
