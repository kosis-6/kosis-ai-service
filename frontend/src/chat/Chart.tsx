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
import type { Chart, ChartView } from "../api/types";
import {
  LINE_COLORS,
  MAX_OVERLAP,
  PART_COLORS,
  axisTick,
  changeText,
  fitsLabel,
  formatValue,
  labelWidth,
  maxAbs,
  panels,
  rowLabel,
  seriesMax,
  seriesRows,
  shortUnit,
  withUnit,
} from "./chartFormat";

const INK = "#173b2e";
const MUTED = "#5f7265";
const GRID = "#e1e9df";
const SERIES = "#245941";
const SURFACE = "#ffffff";
const HOVER = "#e7f1e5";

const TICK = { fontSize: 11, fill: MUTED };

const MOTION = typeof window !== "undefined" && !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const DRAW = { isAnimationActive: MOTION, animationDuration: 700, animationEasing: "ease-out" } as const;

type TipProps = { active?: boolean; payload?: readonly { value?: unknown }[]; label?: unknown };

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
          dot={rows.length <= 24 ? { r: 4, fill: SERIES, stroke: SURFACE, strokeWidth: 2 } : false}
          activeDot={{ r: 6, fill: SERIES, stroke: SURFACE, strokeWidth: 2 }}
          {...DRAW}
        >
          <LabelList
            dataKey="value"
            content={({ x, y, index, value }) =>
              (index === 0 || index === last) && typeof value === "number" ? (
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

function ColumnView({ chart }: { chart: Chart }) {
  const top = maxAbs(chart.rows);
  return (
    <ResponsiveContainer width="100%" height={248}>
      <BarChart data={chart.rows} margin={{ top: 24, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid vertical={false} stroke={GRID} />
        <XAxis dataKey="label" tickLine={false} axisLine={{ stroke: GRID }} tick={TICK} minTickGap={8} />
        <YAxis width={48} tickLine={false} axisLine={false} tick={TICK} tickFormatter={(v: number) => axisTick(v, top)} />
        <Tooltip cursor={{ fill: HOVER }} content={(p) => <Tip {...p} chart={chart} />} />
        <Bar dataKey="value" fill={SERIES} maxBarSize={24} radius={[4, 4, 0, 0]} {...DRAW} />
      </BarChart>
    </ResponsiveContainer>
  );
}

type SeriesTipProps = {
  active?: boolean;
  payload?: readonly { value?: unknown; dataKey?: unknown }[];
  label?: unknown;
};

function SeriesTip({ active, payload, label, chart }: SeriesTipProps & { chart: Chart }) {
  if (!active || !payload?.length) return null;
  const names = chart.series ?? [];
  const items = payload
    .map((p) => ({ i: Number(String(p.dataKey).slice(1)), value: p.value }))
    .filter((p): p is { i: number; value: number } => typeof p.value === "number")
    .sort((a, b) => b.value - a.value);
  if (!items.length) return null;
  return (
    <div className="chart-tip">
      <span>{`${String(label)}년`}</span>
      {items.map(({ i, value }) => (
        <span key={i} className="chart-tip-row">
          <span className="parts-key" style={{ background: LINE_COLORS[i] }} />
          {names[i]}
          <strong>{withUnit(value, chart.unit)}</strong>
        </span>
      ))}
    </div>
  );
}

function SeriesLegend({ chart }: { chart: Chart }) {
  return (
    <ul className="parts-legend chart-legend">
      {(chart.series ?? []).map((name, i) => (
        <li key={name}>
          <span className="line-key" style={{ background: LINE_COLORS[i] }} />
          {name}
        </li>
      ))}
    </ul>
  );
}

function MultiLineView({ chart }: { chart: Chart }) {
  const rows = seriesRows(chart);
  const top = seriesMax(chart);
  const names = chart.series ?? [];
  return (
    <>
      <SeriesLegend chart={chart} />
      <ResponsiveContainer width="100%" height={248}>
        <LineChart data={rows} margin={{ top: 16, right: 28, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis
            dataKey="year"
            type="number"
            domain={["dataMin", "dataMax"]}
            ticks={rows.map((r) => r.year as number)}
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
          <Tooltip cursor={{ stroke: MUTED, strokeWidth: 1 }} content={(p) => <SeriesTip {...p} chart={chart} />} />
          {names.map((name, i) => (
            <Line
              key={name}
              name={name}
              dataKey={`s${i}`}
              stroke={LINE_COLORS[i]}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              dot={rows.length <= 24 ? { r: 4, fill: LINE_COLORS[i], stroke: SURFACE, strokeWidth: 2 } : false}
              activeDot={{ r: 6, fill: LINE_COLORS[i], stroke: SURFACE, strokeWidth: 2 }}
              {...DRAW}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </>
  );
}

type PointTipProps = { active?: boolean; payload?: readonly { value?: unknown }[]; label?: unknown };

function PointTip({ active, payload, label, unit }: PointTipProps & { unit: string }) {
  const value = payload?.[0]?.value;
  if (!active || typeof value !== "number") return null;
  return (
    <div className="chart-tip">
      <strong>{withUnit(value, unit)}</strong>
      <span>{`${String(label)}년`}</span>
    </div>
  );
}

function GridView({ chart }: { chart: Chart }) {
  const all = panels(chart);
  const years = chart.rows.map((r) => Number(r.label));
  const span = years.length ? `${years[0]}~${years[years.length - 1]}년` : "";
  return (
    <>
      <p className="chart-unit">{`지역이 많아 지역마다 따로 그렸습니다. 세로축은 지역마다 다르니 크기는 숫자로 비교해 주세요. 증감률은 ${span} 처음 대비 마지막`}</p>
      <ul className="chart-grid">
        {all.map((panel) => {
          const known = panel.points.filter((p) => p.value != null);
          const last = known[known.length - 1];
          const top = Math.max(0, ...known.map((p) => Math.abs(p.value ?? 0)));
          const label = `${panel.name}: ${known.map((p) => `${p.year}년 ${withUnit(p.value, chart.unit)}`).join(", ")}`;
          return (
            <li key={panel.name} className="chart-cell">
              <div className="chart-cell-head">
                <h4>{panel.name}</h4>
                <span className="chart-cell-change">{changeText(panel.change)}</span>
              </div>
              <p className="chart-cell-value">{last ? `${last.year}년 ${withUnit(last.value, chart.unit)}` : "–"}</p>
              <div role="img" aria-label={label}>
                <ResponsiveContainer width="100%" height={88}>
                  <LineChart data={panel.points} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
                    <XAxis dataKey="year" type="number" domain={["dataMin", "dataMax"]} hide />
                    <YAxis domain={["auto", "auto"]} hide tickFormatter={(v: number) => axisTick(v, top)} />
                    <Tooltip cursor={{ stroke: MUTED, strokeWidth: 1 }} content={(p) => <PointTip {...p} unit={chart.unit} />} />
                    <Line
                      dataKey="value"
                      stroke={SERIES}
                      strokeWidth={2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      dot={false}
                      activeDot={{ r: 5, fill: SERIES, stroke: SURFACE, strokeWidth: 2 }}
                      connectNulls={false}
                      {...DRAW}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}

function MultiColumnView({ chart }: { chart: Chart }) {
  const rows = seriesRows(chart);
  const top = seriesMax(chart);
  const names = chart.series ?? [];
  return (
    <>
      <SeriesLegend chart={chart} />
      <ResponsiveContainer width="100%" height={248}>
        <BarChart data={rows} margin={{ top: 16, right: 12, bottom: 0, left: 0 }} barGap={2}>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="year" tickLine={false} axisLine={{ stroke: GRID }} tick={TICK} minTickGap={8} />
          <YAxis width={48} tickLine={false} axisLine={false} tick={TICK} tickFormatter={(v: number) => axisTick(v, top)} />
          <Tooltip cursor={{ fill: HOVER }} content={(p) => <SeriesTip {...p} chart={chart} />} />
          {names.map((name, i) => (
            <Bar
              key={name}
              name={name}
              dataKey={`s${i}`}
              fill={LINE_COLORS[i]}
              maxBarSize={16}
              radius={[4, 4, 0, 0]}
              {...DRAW}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </>
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
        <Bar dataKey="value" fill={SERIES} maxBarSize={20} radius={[0, 4, 4, 0]} {...DRAW}>
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

function SeriesTableView({ chart }: { chart: Chart }) {
  const names = chart.series ?? [];
  return (
    <table className="chart-table">
      <thead>
        <tr>
          <th scope="col">{chart.category}</th>
          {names.map((name) => (
            <th scope="col" key={name}>
              {shortUnit(chart.unit) ? `${name} (${chart.unit})` : name}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {chart.rows.map((r) => (
          <tr key={r.label}>
            <th scope="row">{rowLabel(chart, r)}</th>
            {names.map((name, i) => (
              <td key={name}>{formatValue(r.values?.[i])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TableView({ chart }: { chart: Chart }) {
  if (chart.series?.length) return <SeriesTableView chart={chart} />;
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

export default function ChartCard({ chart, onViewChange }: { chart: Chart; onViewChange?: (view: ChartView) => void }) {
  // 겹쳐 그릴지는 서버가 정한다(chart.crowded). 값이 없는 예전 대화만 선 수로 센다
  const crowded = chart.crowded ?? (chart.series?.length ?? 0) > MAX_OVERLAP;
  const views: [ChartView, string][] = crowded
    ? [
        ["line", "지역별"],
        ["table", "표"],
      ]
    : chart.type === "line"
      ? [
          ["line", "꺾은선"],
          ["bar", "막대"],
          ["table", "표"],
        ]
      : [
          ["bar", "차트"],
          ["table", "표"],
        ];
  const asked = views.find(([key]) => key === chart.view)?.[0];
  const [view, setView] = useState<ChartView>(asked ?? views[0][0]);

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
              onClick={() => {
                setView(key);
                onViewChange?.(key);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </figcaption>
      <div className="chart-body" key={view}>
      {view === "table" ? (
        <TableView chart={chart} />
      ) : chart.type === "parts" ? (
        <PartsView chart={chart} />
      ) : chart.type === "bar" ? (
        <BarView chart={chart} />
      ) : crowded ? (
        <GridView chart={chart} />
      ) : chart.series?.length ? (
        view === "bar" ? <MultiColumnView chart={chart} /> : <MultiLineView chart={chart} />
      ) : view === "bar" ? (
        <ColumnView chart={chart} />
      ) : (
        <LineView chart={chart} />
      )}
      </div>
      {chart.note && <p className="chart-note">{chart.note}</p>}
    </figure>
  );
}
