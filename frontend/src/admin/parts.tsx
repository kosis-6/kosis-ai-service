import { type ReactNode, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { ApiError, type Who } from "./api";
import { statusLabel } from "./format";

type Loaded<T> = { data: T | null; error: ApiError | null; loading: boolean };

function asApiError(e: unknown): ApiError {
  return e instanceof ApiError ? e : new ApiError(0, "화면에서 기록을 읽지 못했어요.");
}

export function useLoad<T>(load: () => Promise<T>, key: string): Loaded<T> & { reload: () => void } {
  const [state, setState] = useState<Loaded<T>>({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  const latest = useRef(load);
  latest.current = load;
  useEffect(() => {
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    latest.current().then(
      (data) => live && setState({ data, error: null, loading: false }),
      (e) => live && setState({ data: null, error: asApiError(e), loading: false }),
    );
    return () => {
      live = false;
    };
  }, [key, tick]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}

type Page<T> = { rows: T[]; total: number };

export function usePaged<T>(load: (offset: number) => Promise<Page<T>>, key: string) {
  const first = useLoad(() => load(0), key);
  const [extra, setExtra] = useState<T[]>([]);
  const [busy, setBusy] = useState(false);
  const [moreError, setMoreError] = useState<ApiError | null>(null);
  const current = useRef(key);
  useEffect(() => {
    current.current = key;
    setExtra([]);
    setMoreError(null);
    setBusy(false);
  }, [key, first.data]);
  const rows = first.data ? [...first.data.rows, ...extra] : [];
  const total = first.data?.total ?? 0;
  async function more(): Promise<void> {
    const asked = key;
    setBusy(true);
    setMoreError(null);
    try {
      const next = await load(rows.length);
      if (current.current === asked) setExtra((prev) => [...prev, ...next.rows]);
    } catch (e) {
      if (current.current === asked) setMoreError(asApiError(e));
    } finally {
      if (current.current === asked) setBusy(false);
    }
  }
  return { rows, total, loading: first.loading, error: first.error, reload: first.reload, more, busy, moreError };
}

export function useSettled(value: string, ms = 300): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setSettled(value.trim()), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return settled;
}

export function Status({ value, search = false }: { value: string | null | undefined; search?: boolean }) {
  const label = statusLabel(value, search, true);
  return (
    <span className={`chip ${label.tone}`} title={value ?? undefined}>
      {label.text}
    </span>
  );
}

type Option<T> = { value: T; label: string };

export function Choice<T extends string | number>(props: {
  label: string;
  value: T;
  options: Option<T>[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="choice" role="group" aria-label={props.label}>
      {props.options.map((o) => (
        <button
          type="button"
          key={o.value}
          className="choice-item"
          aria-pressed={o.value === props.value}
          onClick={() => props.onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export const DAYS: Option<number>[] = [
  { value: 1, label: "24시간" },
  { value: 7, label: "7일" },
  { value: 30, label: "30일" },
];
export const WHO: Option<Who>[] = [
  { value: "all", label: "전체" },
  { value: "user", label: "로그인 사용자" },
  { value: "guest", label: "비로그인 사용자" },
];

export function Problem({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  if (error.status === 401 || error.status === 403) return <Navigate to="/" replace />;
  return (
    <div className="state" role="alert">
      <p className="state-title">기록을 불러오지 못했어요</p>
      <p className="state-text">{error.message}</p>
      {onRetry && (
        <button type="button" className="btn secondary state-btn" onClick={onRetry}>
          다시 불러오기
        </button>
      )}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="state">
      <p className="state-title">{title}</p>
      <p className="state-text">{children}</p>
    </div>
  );
}

export function SkeletonRows({ n = 8 }: { n?: number }) {
  return (
    <ul className="rows" aria-hidden="true">
      {Array.from({ length: n }, (_, i) => (
        <li className="row skeleton" key={i}>
          <span />
          <span />
          <span />
        </li>
      ))}
    </ul>
  );
}

export function More(props: { shown: number; total: number; busy: boolean; error: ApiError | null; onMore: () => void }) {
  if (props.shown >= props.total) return null;
  return (
    <div className="more">
      {props.error && <p className="more-error">{props.error.message}</p>}
      <button type="button" className="btn secondary more-btn" disabled={props.busy} onClick={props.onMore}>
        {props.busy ? "불러오는 중" : `더 보기 (${props.total - props.shown}개 남음)`}
      </button>
    </div>
  );
}
