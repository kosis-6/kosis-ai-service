// 대화 기록: 기간의 사용량 → 대화 목록 → 고른 대화의 턴 (말한 순서대로)
import { useEffect, useMemo, useState } from "react";
import type { SessionRead, TurnDetail } from "../api/types";
import * as api from "./api";
import { changes, conditionLine, count, day, person, requestLabel, when } from "./format";
import { Choice, DAYS, Empty, More, Problem, SkeletonRows, Status, useLoad, usePaged, useSettled, WHO } from "./parts";

export function SessionsView() {
  const [days, setDays] = useState(7);
  const [who, setWho] = useState<api.Who>("all");
  const [word, setWord] = useState("");
  const q = useSettled(word);
  const [picked, setPicked] = useState<string | null>(null);

  const key = `${days}|${who}|${q}`;
  const since = useMemo(() => api.sinceDays(days), [key]);
  const list = usePaged(
    async (offset) => {
      const found = await api.sessions({ since, who, q }, offset);
      return { rows: found.sessions, total: found.total };
    },
    key,
  );
  // 거르는 조건을 바꾸면 고른 대화를 닫는다 (목록에 없는 대화가 열려 있지 않게)
  useEffect(() => setPicked(null), [key]);

  // 권한 문제는 화면 전체의 문제라 목록 자리 대신 한 번만 보여 준다
  if (list.error && (list.error.status === 401 || list.error.status === 403)) {
    return <Problem error={list.error} />;
  }

  return (
    <>
      <div className="toolbar">
        <Choice label="기간" value={days} options={DAYS} onChange={setDays} />
        <Choice label="사용자" value={who} options={WHO} onChange={setWho} />
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

      <Usage days={days} />

      <div className={`split${picked ? " has-detail" : ""}`}>
        <section className="list" aria-label="대화 목록">
          <div className="list-head">
            <h2 className="list-title">대화</h2>
            {!list.loading && !list.error && <p className="list-count">{count(list.total)}개</p>}
          </div>
          {list.loading ? (
            <SkeletonRows />
          ) : list.error ? (
            <Problem error={list.error} onRetry={list.reload} />
          ) : list.rows.length === 0 ? (
            <Empty title="이 조건에 맞는 대화가 없어요">
              기간을 넓히거나 찾는 말을 지워 보세요. 대화 기록은 30일 동안 보관돼요.
            </Empty>
          ) : (
            <>
              <ul className="rows">
                {list.rows.map((s) => (
                  <SessionRow key={s.session_id} row={s} open={s.session_id === picked} onOpen={setPicked} />
                ))}
              </ul>
              <More shown={list.rows.length} total={list.total} busy={list.busy} error={list.moreError} onMore={list.more} />
            </>
          )}
        </section>

        {picked && <Conversation id={picked} onClose={() => setPicked(null)} />}
      </div>
    </>
  );
}

function SessionRow({ row, open, onOpen }: { row: SessionRead; open: boolean; onOpen: (id: string) => void }) {
  return (
    <li>
      <button type="button" className="row session-row" aria-pressed={open} onClick={() => onOpen(row.session_id)}>
        <span className="row-main">{row.first_message}</span>
        <span className="row-meta">
          <span>{when(row.last_at)}</span>
          <span>{person(row)}</span>
          <span>{row.turns}턴</span>
        </span>
        <Status value={row.last_status} />
      </button>
    </li>
  );
}

/** 기간의 사용량: 숫자 한 줄 + 날짜별·결과별 개수 */
function Usage({ days }: { days: number }) {
  const { data, error, loading } = useLoad(() => api.summary(days), String(days));
  if (error) return null; // 목록이 같은 문제를 알려 준다
  const facts: [string, number | null][] = [
    ["질문", data?.turns ?? null],
    ["대화", data?.sessions ?? null],
    ["로그인 사용자", data?.users ?? null],
    ["손님 질문", data?.guest_turns ?? null],
    ["통계표 찾기", data?.searches ?? null],
    ["찾기 오류", data?.search_errors ?? null],
  ];
  return (
    <section className="usage" aria-label="사용량" aria-busy={loading}>
      <dl className="facts">
        {facts.map(([name, n]) => (
          <div className="fact" key={name}>
            <dt>{name}</dt>
            <dd>{n === null ? "–" : count(n)}</dd>
          </div>
        ))}
      </dl>
      {data && data.turns > 0 && (
        <div className="breakdowns">
          <table className="mini">
            <caption>날짜별</caption>
            <thead>
              <tr>
                <th scope="col">날짜</th>
                <th scope="col">질문</th>
                <th scope="col">대화</th>
              </tr>
            </thead>
            <tbody>
              {data.by_day.map((d) => (
                <tr key={d.day}>
                  <th scope="row">{day(d.day)}</th>
                  <td>{count(d.turns)}</td>
                  <td>{count(d.sessions)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="tally">
            <p className="tally-title">결과별</p>
            <ul className="tally-list">
              {data.by_status.map((s) => (
                <li key={s.name}>
                  <Status value={s.name || null} />
                  <span>{count(s.count)}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="tally">
            <p className="tally-title">요청 종류별</p>
            <ul className="tally-list">
              {data.by_request_type.map((t) => (
                <li key={t.name}>
                  <span className="chip plain">{requestLabel(t.name) || "기록 없음"}</span>
                  <span>{count(t.count)}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </section>
  );
}

function Conversation({ id, onClose }: { id: string; onClose: () => void }) {
  const { data, error, loading, reload } = useLoad(() => api.session(id), id);
  return (
    <section className="detail" aria-label="대화 내용">
      <div className="detail-head">
        <div>
          <h2 className="detail-title">{data ? person(data) : "대화"}</h2>
          <p className="detail-sub">
            {data && `${when(data.turns[0].created_at)} 시작 · ${data.turns.length}턴 · `}
            <span className="code">{id}</span>
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
        <ol className="turns">{data?.turns.map((t) => <Turn key={t.id} turn={t} />)}</ol>
      )}
    </section>
  );
}

function Turn({ turn }: { turn: TurnDetail }) {
  const changed = changes(turn.conditions_before as never, turn.conditions_after as never);
  const read = changed.length ? "" : conditionLine(turn.conditions_after as never);
  return (
    <li className="turn">
      <p className="turn-meta">
        <span>{when(turn.created_at)}</span>
        {turn.request_type && <span className="chip plain">{requestLabel(turn.request_type)}</span>}
        <Status value={turn.status} />
        {!turn.recomputed && <span className="turn-note">다시 계산하지 않음</span>}
      </p>
      <p className="turn-said">{turn.message}</p>
      {turn.answer && <p className="turn-answer">{turn.answer}</p>}
      {changed.length > 0 && (
        <ul className="turn-changes">
          {changed.map((c) => (
            <li key={c.name}>
              <span className="change-name">{c.name}</span>
              <span className="change-from">{c.from}</span>
              <span aria-hidden="true">→</span>
              <span className="sr-only">에서</span>
              <span className="change-to">{c.to}</span>
            </li>
          ))}
        </ul>
      )}
      {(read || turn.tbl_id) && (
        <p className="turn-read">
          {read && <span>읽은 조건: {read}</span>}
          {turn.tbl_id && <span className="code">{turn.tbl_id}</span>}
        </p>
      )}
    </li>
  );
}
