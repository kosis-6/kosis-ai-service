// 통계표 찾기 기록: 목록 → 고른 기록의 슬롯·후보 점수·단계별 시간
import { useEffect, useState } from "react";
import type { SearchLogRead } from "../api/types";
import * as api from "./api";
import { count, filled, person, seconds, statusLabel, when } from "./format";
import { Choice, DAYS, Empty, More, Problem, SkeletonRows, Status, useLoad, usePaged, useSettled, WHO } from "./parts";

const STATUSES = ["ok", "partial_match", "ambiguous", "clarify", "out_of_scope", "not_statistics", "parse_error"];

export function SearchesView() {
  const [days, setDays] = useState(7);
  const [who, setWho] = useState<api.Who>("all");
  const [status, setStatus] = useState("");
  const [errors, setErrors] = useState(false);
  const [word, setWord] = useState("");
  const q = useSettled(word);
  const [picked, setPicked] = useState<number | null>(null);

  const key = `${days}|${who}|${status}|${errors}|${q}`;
  const list = usePaged(
    async (offset) => {
      const found = await api.searches({ days, who, q, status, errors }, offset);
      return { rows: found.searches, total: found.total };
    },
    key,
  );
  useEffect(() => setPicked(null), [key]);

  if (list.error && (list.error.status === 401 || list.error.status === 403)) {
    return <Problem error={list.error} />;
  }

  return (
    <>
      <div className="toolbar">
        <Choice label="기간" value={days} options={DAYS} onChange={setDays} />
        <Choice label="사용자" value={who} options={WHO} onChange={setWho} />
        <label className="pick">
          <span className="sr-only">결과</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">모든 결과</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {statusLabel(s, true).text}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={errors} onChange={(e) => setErrors(e.target.checked)} />
          오류만
        </label>
        <label className="find">
          <span className="sr-only">질문에 들어 있는 말</span>
          <input
            type="search"
            value={word}
            maxLength={100}
            placeholder="질문에 들어 있는 말"
            onChange={(e) => setWord(e.target.value)}
          />
        </label>
      </div>

      <div className={`split${picked ? " has-detail" : ""}`}>
        <section className="list" aria-label="통계표 찾기 기록 목록">
          <div className="list-head">
            <h2 className="list-title">통계표 찾기</h2>
            {!list.loading && !list.error && <p className="list-count">{count(list.total)}개</p>}
          </div>
          {list.loading ? (
            <SkeletonRows />
          ) : list.error ? (
            <Problem error={list.error} onRetry={list.reload} />
          ) : list.rows.length === 0 ? (
            <Empty title="이 조건에 맞는 기록이 없어요">기간을 넓히거나 거르는 조건을 풀어 보세요.</Empty>
          ) : (
            <>
              <ul className="rows">
                {list.rows.map((s) => (
                  <SearchRow key={s.id} row={s} open={s.id === picked} onOpen={setPicked} />
                ))}
              </ul>
              <More shown={list.rows.length} total={list.total} busy={list.busy} error={list.moreError} onMore={list.more} />
            </>
          )}
        </section>

        {picked !== null && <SearchDetail id={picked} onClose={() => setPicked(null)} />}
      </div>
    </>
  );
}

function SearchRow({ row, open, onOpen }: { row: SearchLogRead; open: boolean; onOpen: (id: number) => void }) {
  return (
    <li>
      <button type="button" className="row session-row" aria-pressed={open} onClick={() => onOpen(row.id)}>
        <span className="row-main">{row.question}</span>
        <span className="row-meta">
          <span>{when(row.created_at)}</span>
          <span>{person(row)}</span>
          {row.elapsed_ms != null && <span>{seconds(row.elapsed_ms)}</span>}
          {row.tbl_id && <span className="code">{row.tbl_id}</span>}
        </span>
        {row.error ? <span className="chip bad">오류</span> : <Status value={row.status} search />}
      </button>
    </li>
  );
}

type Slots = {
  topic?: string | null;
  search_query?: string | null;
  target?: string | null;
  exclude?: string[];
  conditions?: unknown;
  fixes?: { key: string; from: unknown; to: unknown; reason?: string }[];
};
type Candidate = { tbl_id: string; table_name?: string; score?: number | null; keyword?: number | null; dense?: number | null };

const text = (v: unknown): string => (v === null || v === undefined || v === "" ? "없음" : typeof v === "string" ? v : JSON.stringify(v));
const score = (n: number | null | undefined): string => (n == null ? "" : n.toFixed(3));

function SearchDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, error, loading, reload } = useLoad(() => api.search(id), String(id));
  const slots = (data?.slots ?? null) as Slots | null;
  const candidates = (Array.isArray(data?.candidates) ? data.candidates : []) as Candidate[];
  const timings = Object.entries((data?.timings ?? {}) as Record<string, number>);
  const read: [string, string][] = slots
    ? [
        ...(["topic", "search_query", "target"] as const).flatMap((k): [string, string][] => (slots[k] ? [[k, String(slots[k])]] : [])),
        ...(slots.exclude?.length ? ([["exclude", slots.exclude.join(", ")]] as [string, string][]) : []),
        ...filled(slots.conditions),
      ]
    : [];
  return (
    <section className="detail" aria-label="통계표 찾기 기록">
      <div className="detail-head">
        <div>
          <h2 className="detail-title">{data?.question ?? "통계표 찾기"}</h2>
          <p className="detail-sub">
            {data && `${when(data.created_at)} · ${person(data)} · `}
            <span className="code">#{id}</span>
          </p>
        </div>
        <button type="button" className="btn quiet detail-close" onClick={onClose}>
          닫기
        </button>
      </div>
      {loading ? (
        <SkeletonRows n={4} />
      ) : error ? (
        <Problem error={error} onRetry={reload} />
      ) : (
        data && (
          <div className="detail-body">
            {data.error && (
              <p className="error-text" role="alert">
                {data.error}
              </p>
            )}
            <h3 className="part">읽은 조건</h3>
            {read.length ? (
              <dl className="pairs">
                {read.map(([k, v]) => (
                  <div key={k}>
                    <dt className="code">{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="part-none">조건을 읽은 기록이 없어요.</p>
            )}
            {!!slots?.fixes?.length && (
              <>
                <h3 className="part">코드가 고친 것</h3>
                <ul className="fixes">
                  {slots.fixes.map((f, i) => (
                    <li key={i}>
                      <span className="code">{f.key}</span> {text(f.from)} <span aria-hidden="true">→</span>
                      <span className="sr-only">에서</span> {text(f.to)}
                      {f.reason && <span className="fix-why"> ({f.reason})</span>}
                    </li>
                  ))}
                </ul>
              </>
            )}
            <h3 className="part">후보 {candidates.length}개</h3>
            {candidates.length ? (
              <div className="scroll-x">
                <table className="mini wide">
                  <thead>
                    <tr>
                      <th scope="col">순위</th>
                      <th scope="col">통계표</th>
                      <th scope="col">점수</th>
                      <th scope="col">키워드</th>
                      <th scope="col">임베딩</th>
                    </tr>
                  </thead>
                  <tbody>
                    {candidates.map((c, i) => (
                      <tr key={c.tbl_id} className={c.tbl_id === data.tbl_id ? "is-picked" : undefined}>
                        <td>{i + 1}</td>
                        <th scope="row">
                          {c.table_name ?? c.tbl_id}
                          <span className="code cell-id">{c.tbl_id}</span>
                        </th>
                        <td>{score(c.score)}</td>
                        <td>{score(c.keyword)}</td>
                        <td>{score(c.dense)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="part-none">후보가 남지 않았어요.</p>
            )}
            {timings.length > 0 && (
              <>
                <h3 className="part">단계별 시간</h3>
                <dl className="pairs">
                  {timings.map(([k, ms]) => (
                    <div key={k}>
                      <dt className="code">{k}</dt>
                      <dd>{typeof ms === "number" ? `${count(Math.round(ms))}ms` : text(ms)}</dd>
                    </div>
                  ))}
                </dl>
              </>
            )}
          </div>
        )
      )}
    </section>
  );
}
