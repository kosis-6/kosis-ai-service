// 채팅 화면. 왼쪽 메뉴로 두 가지를 오간다 (mode)
//   search  통계표 찾기: POST /search 응답(status별)을 말풍선·표 카드·선택 버튼으로 그린다
//   ask     통계 수치 묻기: POST /ask 응답을 답변 문장·차트·출처로 그린다
// 목업 모드(?mock=1)는 chat/mock.js·mockAsk.ts 가 응답을 만들어 서버·HCX 를 부르지 않는다
// 개발 도구(목업·디버그 전환, 검색 과정)는 ?mock=1 / ?debug=1 로 켰을 때만 보인다
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, NavLink } from "react-router-dom";
import type { AskResponse, Parsed, Recommendation, SearchRequest, SearchResponse } from "../api/types";
import iconArrow from "../assets/icon-arrow.svg";
import iconChart from "../assets/icon-chart.svg";
import iconSearch from "../assets/icon-search.svg";
import iconSpark from "../assets/icon-spark.svg";
import { logout } from "../auth/client";
import { useUser } from "../auth/useUser";
import { Answer } from "../chat/Answer";
import { ask, search } from "../chat/api";
import { ASK_CATEGORIES, ASK_EXAMPLES, EXAMPLE_CATEGORIES, EXAMPLES } from "../chat/examples";
import { AltCard, Reply } from "../chat/Reply";
import { DevTools, Sheet } from "../chat/Sheet";
import { Brand } from "../components/Brand";
import { usePage } from "../components/usePage";
import "../styles/chat.css";

type Message =
  | { id: number; kind: "user"; text: string }
  | { id: number; kind: "bot"; data: SearchResponse }
  | { id: number; kind: "answer"; data: AskResponse }
  | { id: number; kind: "error"; text: string };

type NewMessage = Message extends infer M ? (M extends Message ? Omit<M, "id"> : never) : never;

type SheetState =
  | { kind: "others"; recs: Recommendation[]; parsed: Parsed }
  | { kind: "examples" }
  | { kind: "dev"; data: SearchResponse | null };

// 보기 편의용 설정이라 저장에 실패해도 동작한다
function load(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function save(key: string, on: boolean): void {
  try {
    localStorage.setItem(key, on ? "1" : "0");
  } catch {
    /* 저장 못 해도 화면은 동작 */
  }
}
function initial(name: "mock" | "debug"): boolean {
  const params = new URLSearchParams(location.search);
  return params.has(name) ? params.get(name) !== "0" : load(`chat.${name}`) === "1";
}

export type Mode = "search" | "ask";

// 화면마다 다른 글자와 질문 예시
const MODES = {
  search: {
    title: "인구 통계표 안내",
    categories: EXAMPLE_CATEGORIES,
    examples: EXAMPLES,
    placeholder: "궁금한 인구 통계를 편하게 물어보세요",
    typing: "통계표를 찾고 있어요",
    wide: ["평소 말투로 물어보세요. 인구 분야 통계표 2,364개에서", "질문에 맞는 자료와 KOSIS 바로가기를 안내해 드려요."],
    narrow: ["평소 말투로 물어보세요.", "인구 분야 통계표 2,364개에서 찾아드려요."],
    note: ["통계표의 수치와 상세 내용은 KOSIS에서 확인해 주세요.", "수치와 상세 내용은 KOSIS에서 확인해 주세요."],
  },
  ask: {
    title: "인구 통계 수치",
    categories: ASK_CATEGORIES,
    examples: ASK_EXAMPLES,
    placeholder: "지역과 연도를 넣어 수치를 물어보세요",
    typing: "통계를 조회하고 있어요",
    wide: ["지역과 연도를 넣어 물어보세요. KOSIS 통계표의 수치를", "문장과 차트, 출처와 함께 보여 드려요."],
    narrow: ["지역과 연도를 넣어 물어보세요.", "수치를 차트, 출처와 함께 보여 드려요."],
    note: ["수치는 KOSIS 통계표에서 조회·계산한 값입니다. 출처에서 확인해 주세요.", "수치는 KOSIS 통계표 기준입니다."],
  },
} as const;

export function ChatPage({ mode }: { mode: Mode }) {
  const text = MODES[mode];
  const mainExamples = text.examples.slice(0, text.categories.length);
  const moreExamples = text.examples.slice(text.categories.length);
  usePage("chat", text.title);
  const user = useUser();

  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState("");
  const [sheet, setSheet] = useState<SheetState | null>(null);
  const [mock, setMock] = useState(() => initial("mock"));
  const [debug, setDebug] = useState(() => initial("debug"));

  const sessionId = useRef<string | null>(null);
  const nextId = useRef(1);
  const busyRef = useRef(false); // 같은 순간의 두 번 보내기를 막는다 (state 는 다음 그리기에야 바뀐다)
  const logRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const inChat = messages.length > 0;

  useEffect(() => save("chat.mock", mock), [mock]);
  useEffect(() => save("chat.debug", debug), [debug]);
  // 대화 중에만 보이는 버튼(모바일의 "새 대화")을 CSS 가 body.in-chat 으로 가린다
  useEffect(() => {
    document.body.classList.toggle("in-chat", inChat);
    return () => document.body.classList.remove("in-chat");
  }, [inChat]);
  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [messages, busy]);
  useEffect(() => inputRef.current?.focus(), []);

  function add(message: NewMessage) {
    setMessages((prev) => [...prev, { ...message, id: nextId.current++ } as Message]);
  }

  async function send(text: string, selections?: Record<string, string>) {
    const q = text.trim();
    if (!q || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    add({ kind: "user", text: q });
    try {
      if (mode === "ask") {
        // 연도를 되물었으면(missing_year) 서버가 session_id 를 준다. 다음 답("2000년")과 합쳐 준다
        const data = await ask({ question: q, session_id: sessionId.current }, mock);
        sessionId.current = data.session_id ?? null;
        add({ kind: "answer", data });
      } else {
        const body: SearchRequest = { question: q, session_id: sessionId.current, top_k: 3, debug };
        if (selections && Object.keys(selections).length) body.selections = selections;
        const data = await search(body, mock);
        sessionId.current = data.session_id;
        add({ kind: "bot", data });
      }
    } catch (e) {
      add({ kind: "error", text: `답변을 가져오지 못했어요. ${e instanceof Error ? e.message : String(e)}` });
    } finally {
      busyRef.current = false;
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  function reset() {
    sessionId.current = null;
    setMessages([]);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const q = question;
    setQuestion("");
    if (inputRef.current) inputRef.current.style.height = "";
    void send(q);
  }
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    }
  }
  // 입력한 줄 수에 맞춰 입력창 높이를 늘린다 (최대 140px)
  function grow(el: HTMLTextAreaElement) {
    el.style.height = "";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }

  return (
    <>
      <aside className="sidebar">
        <div className="side-top">
          <Brand />
          <button type="button" className="btn primary block side-new" onClick={reset}>
            {"＋  새 대화 시작하기"}
          </button>
          <p className="side-label">WORKSPACE</p>
          <nav className="menu" aria-label="하는 일">
            <NavLink className="menu-item" to="/" end>
              <img src={iconSearch} width="18" height="18" alt="" />
              통계표 찾기
            </NavLink>
            <NavLink className="menu-item" to="/ask">
              <img src={iconChart} width="18" height="18" alt="" />
              통계 수치 묻기
            </NavLink>
          </nav>
        </div>
        <div className="side-bottom">
          <div className="guide">
            <span className="guide-icon">
              <img src={iconChart} width="18" height="18" alt="" />
            </span>
            <p className="guide-title">
              궁금한 통계,
              <br />
              시작은 질문 하나로.
            </p>
            <p className="guide-text">
              주제와 기간, 대상을 함께 알려주면
              <br />더 알맞은 통계표를 찾을 수 있어요.
            </p>
            <hr />
            <a className="guide-link" href="https://kosis.kr" target="_blank" rel="noopener">
              KOSIS 국가통계포털
            </a>
          </div>
          {user ? (
            <div className="account">
              <span className="account-mark" aria-hidden="true">
                S
              </span>
              <span className="account-name">{user.name}</span>
              <button type="button" className="text-btn" onClick={() => void logout()}>
                로그아웃
              </button>
            </div>
          ) : (
            <p className="made-by">MADE BY TEAM SIX</p>
          )}
        </div>
      </aside>

      <div className="main">
        <header className="head">
          <div className="service only-wide">
            <p className="service-tag">통계와 일상을 잇다</p>
            <h1 className="service-name">{text.title}</h1>
          </div>
          <Brand className="only-narrow" />
          <div className="head-actions">
            {(mock || debug) && (
              <button type="button" className="dev-badge" onClick={() => setSheet({ kind: "dev", data: null })}>
                {mock ? "목업 모드" : "개발 도구"}
              </button>
            )}
            <p className="source only-wide">
              <span aria-hidden="true">●</span>
              {"  KOSIS 데이터 기반"}
            </p>
            <button type="button" className="btn quiet head-btn only-narrow in-chat-only" onClick={reset}>
              새 대화
            </button>
            {user ? (
              <>
                <p className="head-name only-wide">
                  <span>{user.name}</span>님
                </p>
                <button type="button" className="btn quiet head-btn only-narrow" onClick={() => void logout()}>
                  로그아웃
                </button>
              </>
            ) : (
              <>
                <Link className="btn quiet head-btn" to="/login">
                  로그인
                </Link>
                <Link className="btn primary head-btn only-wide" to="/signup">
                  회원가입
                </Link>
              </>
            )}
          </div>
        </header>

        {/* 좁은 화면에는 사이드바가 없어 메뉴를 머리글 아래에 둔다 */}
        <nav className="mode-tabs only-narrow" aria-label="하는 일">
          <NavLink to="/" end>
            통계표 찾기
          </NavLink>
          <NavLink to="/ask">통계 수치 묻기</NavLink>
        </nav>

        <main className="log" aria-live="polite" ref={logRef}>
          {!inChat && (
            <section className="welcome">
              <span className="emblem">
                <span className="logo" role="img" aria-label="TEAM SIX 로고" />
              </span>
              <p className="welcome-tag">일상 속 궁금증, 통계에서 답을 찾다</p>
              <h2 className="welcome-title">
                어떤 통계가
                <br />
                <em>궁금하신가요?</em>
              </h2>
              <p className="welcome-text only-wide">
                {text.wide[0]}
                <br />
                {text.wide[1]}
              </p>
              <p className="welcome-text only-narrow">
                {text.narrow[0]}
                <br />
                {text.narrow[1]}
              </p>
              <div className="examples">
                <p className="examples-label">이런 질문으로 시작해 보세요</p>
                <div className="examples-grid">
                  {mainExamples.map((q, i) => (
                    <button type="button" className="example" key={q} onClick={() => void send(q)}>
                      <span className="example-cat">{`${text.categories[i]}  ↗`}</span>
                      <span className="example-q">{q}</span>
                    </button>
                  ))}
                </div>
              </div>
              <button type="button" className="text-btn more-examples" onClick={() => setSheet({ kind: "examples" })}>
                {`다른 질문 예시  ${moreExamples.length}  ⌄`}
              </button>
            </section>
          )}

          {messages.map((m) =>
            m.kind === "user" ? (
              <div className="msg user" key={m.id}>
                <div className="bubble">{m.text}</div>
              </div>
            ) : (
              <div className={m.kind === "error" ? "msg bot error" : "msg bot"} key={m.id}>
                <span className="avatar" aria-hidden="true">
                  6
                </span>
                <div className="reply">
                  {m.kind === "error" ? (
                    <div className="bubble">{m.text}</div>
                  ) : m.kind === "answer" ? (
                    <Answer data={m.data} />
                  ) : (
                    <Reply
                      data={m.data}
                      busy={busy}
                      onSend={(text, selections) => void send(text, selections)}
                      onOthers={(recs, parsed) => setSheet({ kind: "others", recs, parsed })}
                      onDebug={(data) => setSheet({ kind: "dev", data })}
                      onAskAgain={() => inputRef.current?.focus()}
                    />
                  )}
                </div>
              </div>
            ),
          )}

          {busy && (
            <div className="msg bot typing">
              <span className="avatar" aria-hidden="true">
                6
              </span>
              <div className="reply">
                <div className="bubble">
                  {text.typing}
                  <span className="dots" />
                </div>
              </div>
            </div>
          )}
        </main>

        <form className="composer" onSubmit={submit}>
          <div className="composer-box">
            <img src={iconSpark} width="20" height="20" alt="" />
            <textarea
              ref={inputRef}
              rows={1}
              maxLength={500}
              placeholder={text.placeholder}
              aria-label="질문"
              required
              value={question}
              onChange={(e) => {
                setQuestion(e.target.value);
                grow(e.target);
              }}
              onKeyDown={onKeyDown}
            />
            <button type="submit" className="send" aria-label="보내기" disabled={busy}>
              <img src={iconArrow} width="21" height="21" alt="" />
            </button>
          </div>
          <p className="composer-note">
            <span className="only-wide">{text.note[0]}</span>
            <span className="only-narrow">{text.note[1]}</span>
            <span className="composer-keys only-wide">Enter 전송 · Shift + Enter 줄바꿈</span>
          </p>
        </form>
      </div>

      {sheet?.kind === "others" && (
        <Sheet title="다른 후보" onClose={() => setSheet(null)}>
          <p className="sheet-text">같은 주제의 다른 통계표도 확인해 보세요.</p>
          {sheet.recs.map((r) => (
            <AltCard key={r.tbl_id} r={r} parsed={sheet.parsed} />
          ))}
        </Sheet>
      )}
      {sheet?.kind === "examples" && (
        <Sheet title="다른 질문 예시" narrow onClose={() => setSheet(null)}>
          {moreExamples.map((q) => (
            <button
              type="button"
              className="btn secondary"
              key={q}
              onClick={() => {
                setSheet(null);
                void send(q);
              }}
            >
              {q}
            </button>
          ))}
        </Sheet>
      )}
      {sheet?.kind === "dev" && (
        <Sheet title="개발 도구" onClose={() => setSheet(null)}>
          <DevTools
            data={sheet.data}
            mock={mock}
            debug={debug}
            onMock={() => {
              // 목업과 실제 답이 한 대화에 섞이지 않게 새 대화로 시작한다
              setMock((on) => !on);
              setSheet(null);
              reset();
            }}
            onDebug={() => setDebug((on) => !on)}
          />
        </Sheet>
      )}
    </>
  );
}
