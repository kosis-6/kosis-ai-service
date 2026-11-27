import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import type { AskResponse, ChartView, ConversationRead, Parsed, Recommendation } from "../api/types";
import { logout } from "../auth/client";
import { useUser } from "../auth/useUser";
import { Answer } from "../chat/Answer";
import { ask } from "../chat/api";
import { EXAMPLE_CATEGORIES, EXAMPLES } from "../chat/examples";
import { History } from "../chat/History";
import { createChartViewQueue, deleteConversation, getConversation, HistoryError, lastChartView, listConversations, replayed } from "../chat/conversations";
import { AltCard } from "../chat/Reply";
import { ReportPreview, ReportSheet } from "../chat/ReportSheet";
import type { Report } from "../chat/reports";
import { Sheet } from "../chat/Sheet";
import { Brand } from "../components/Brand";
import { Icon } from "../components/Icon";
import { Mascot, MascotFace } from "../components/Mascot";
import { usePage } from "../components/usePage";
import "../styles/chat.css";

type Message =
  | { id: number; kind: "user"; text: string }
  | { id: number; kind: "answer"; data: AskResponse }
  | { id: number; kind: "error"; text: string };

type NewMessage = Message extends infer M ? (M extends Message ? Omit<M, "id"> : never) : never;

type SheetState =
  | { kind: "others"; recs: Recommendation[]; parsed: Parsed }
  | { kind: "examples" }
  | { kind: "history" }
  | { kind: "report" }
  | { kind: "report-view"; report: Report };

const TEXT = {
  title: "인구 통계 안내",
  placeholder: "궁금한 인구 통계를 편하게 물어보세요",
  typing: "통계를 찾고 있어요",
  wide: ["평소 말투로 물어보세요. 수치는 차트와 함께,", "질문에 맞는 KOSIS 통계표와 출처까지 안내해 드려요."],
  narrow: ["평소 말투로 물어보세요.", "수치·차트와 통계표를 함께 찾아드려요."],
  note: ["수치는 KOSIS 통계표에서 조회·계산한 값입니다. 출처에서 확인해 주세요.", "수치는 KOSIS 통계표 기준입니다."],
} as const;

const MAIN_EXAMPLES = EXAMPLES.slice(0, EXAMPLE_CATEGORIES.length);
const MORE_EXAMPLES = EXAMPLES.slice(EXAMPLE_CATEGORIES.length);

export function ChatPage() {
  const text = TEXT;
  usePage("chat", text.title);
  const user = useUser();
  const navigate = useNavigate();
  const { sessionId: routeId = null } = useParams();

  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState(false);
  const [question, setQuestion] = useState("");
  const [sheet, setSheet] = useState<SheetState | null>(null);
  const [history, setHistory] = useState<ConversationRead[] | null>(null);
  const [chartViews] = useState(createChartViewQueue);

  const saved = user !== null;

  const sessionId = useRef<string | null>(null);
  const view = useRef(0);
  const nextId = useRef(1);
  const busyRef = useRef(false);
  const logRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const inChat = messages.length > 0 || opening;
  const answers = messages.flatMap((m) => (m.kind === "answer" ? [m.data] : []));
  // 보고서는 로그인한 사용자가 저장된 대화(주소에 대화 id)의 답으로 만든다
  const canMakeReport = saved && routeId !== null && answers.length > 0 && !busy && !opening;

  useEffect(() => {
    document.body.classList.toggle("in-chat", inChat);
    return () => document.body.classList.remove("in-chat");
  }, [inChat]);
  useEffect(() => {
    const log = logRef.current;
    if (!log) return;
    const last = log.querySelector(".msg:last-of-type");
    if (last?.classList.contains("bot") && !busy) {
      log.scrollTop = Math.max(0, (last as HTMLElement).offsetTop - log.offsetTop - 16);
    } else {
      log.scrollTop = log.scrollHeight;
    }
  }, [messages, busy]);
  useEffect(() => inputRef.current?.focus(), []);

  function add(message: NewMessage) {
    setMessages((prev) => [...prev, { ...message, id: nextId.current++ } as Message]);
  }

  function changeChartView(messageId: number, chartView: ChartView) {
    setMessages((prev) => prev.map((message) =>
      message.id === messageId && message.kind === "answer" && message.data.chart
        ? { ...message, data: { ...message.data, chart: { ...message.data.chart, view: chartView } } }
        : message,
    ));
    // 서버의 답에도 남긴다: 말로 만든 보고서와 다시 연 대화가 이 모양을 쓴다.
    // 남기지 못해도 화면은 그대로 두고, [보고서] 시트는 chart_views로 따로 보낸다
    const target = messages.find((message) => message.id === messageId);
    const data = target?.kind === "answer" ? target.data : null;
    const sessionOf = data?.session_id ?? routeId;
    if (saved && sessionOf && data?.turn_id != null && data.chart) {
      void chartViews.save(sessionOf, data.turn_id, chartView).catch(() => {});
    }
  }

  const refreshHistory = useCallback(async () => {
    if (!saved) return;
    try {
      setHistory((await listConversations()).conversations);
    } catch {
    }
  }, [saved]);
  useEffect(() => {
    if (saved) void refreshHistory();
    else setHistory(null);
  }, [saved, refreshHistory]);

  function clear(id: string | null = null) {
    view.current += 1;
    sessionId.current = id;
    busyRef.current = false;
    setBusy(false);
    setOpening(false);
    setMessages([]);
  }

  async function open(id: string) {
    clear(id);
    const v = view.current;
    setOpening(true);
    try {
      await chartViews.wait(id);
      if (v !== view.current) return;
      const found = await getConversation(id);
      if (v !== view.current) return;
      const restored: Message[] = [];
      for (const turn of found.turns) {
        restored.push({ id: nextId.current++, kind: "user", text: turn.question });
        restored.push({ id: nextId.current++, kind: "answer", data: replayed(turn, found.session_id) });
      }
      setMessages(restored);
    } catch (e) {
      if (v !== view.current) return;
      sessionId.current = null;
      navigate("/", { replace: true });
      if (!(e instanceof HistoryError && e.status === 401)) {
        add({ kind: "error", text: e instanceof Error ? e.message : String(e) });
      }
    } finally {
      if (v === view.current) setOpening(false);
    }
  }

  useEffect(() => {
    if (routeId === null) {
      if (sessionId.current !== null && saved) clear();
      return;
    }
    if (routeId === sessionId.current) return;
    void open(routeId);
  }, [routeId]);

  const signedIn = user !== null;
  const wasSignedIn = useRef(signedIn);
  useEffect(() => {
    if (wasSignedIn.current && !signedIn) reset();
    wasSignedIn.current = signedIn;
  }, [signedIn]);

  async function send(text: string, selections?: Record<string, string>) {
    const q = text.trim();
    if (!q || busyRef.current || opening) return;
    const v = view.current;
    const sid = sessionId.current;
    busyRef.current = true;
    setBusy(true);
    add({ kind: "user", text: q });
    try {
      if (sid) await chartViews.wait(sid);
      if (v !== view.current) return;
      const has = selections && Object.keys(selections).length > 0;
      // 로그인한 대화는 서버가 답에 저장한 보기를 읽는다. 로그인하지 않았으면 화면의 보기를 보낸다
      const chartView = saved ? undefined : lastChartView(answers);
      const data = await ask({
        question: q,
        session_id: sid,
        ...(has ? { selections } : {}),
        ...(chartView ? { chart_view: chartView } : {}),
      });
      if (v !== view.current) {
        void refreshHistory();
        return;
      }
      sessionId.current = data.session_id ?? null;
      add({ kind: "answer", data });
      if (saved && data.session_id) {
        if (data.session_id !== routeId) navigate(`/c/${data.session_id}`, { replace: true });
        void refreshHistory();
      }
    } catch (e) {
      if (v !== view.current) return;
      add({ kind: "error", text: `답변을 가져오지 못했어요. ${e instanceof Error ? e.message : String(e)}` });
    } finally {
      if (v === view.current) {
        busyRef.current = false;
        setBusy(false);
        inputRef.current?.focus();
      }
    }
  }

  function reset() {
    clear();
    if (routeId !== null) navigate("/");
    inputRef.current?.focus();
  }

  function openFromList(id: string) {
    setSheet(null);
    if (id !== routeId) navigate(`/c/${id}`);
  }

  async function remove(id: string) {
    try {
      await deleteConversation(id);
    } catch {
      void refreshHistory();
      return;
    }
    setHistory((prev) => prev?.filter((c) => c.session_id !== id) ?? null);
    if (id === sessionId.current) reset();
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
            <Icon name="plus" />새 대화
          </button>
          <nav className="menu" aria-label="메뉴">
            <div className="menu-item" aria-current="page">
              <Icon name="search" size={18} />
              통계 찾기
            </div>
            {user?.admin && (
              <Link className="menu-item" to="/admin">
                <Icon name="chart" size={18} />
                사용 기록
              </Link>
            )}
          </nav>
        </div>
        {saved && (
          <section className="history" aria-labelledby="history-title">
            <h2 className="history-head" id="history-title">
              지난 대화
            </h2>
            <History items={history} current={routeId} onOpen={openFromList} onDelete={(id) => void remove(id)} />
          </section>
        )}
        <div className="side-bottom">
          <div className="guide">
            <span className="guide-icon">
              <Icon name="chart" size={18} />
            </span>
            <p className="guide-title">
              궁금한 통계,
              <br />
              시작은 질문 하나로
            </p>
            <p className="guide-text">
              주제와 기간, 대상을 함께 알려주면 더 알맞은 통계표를 찾을 수 있어요.
            </p>
            <hr />
            <a className="guide-link" href="https://kosis.kr" target="_blank" rel="noopener">
              KOSIS 국가통계포털
              <Icon name="external" size={14} />
            </a>
          </div>
          {user ? (
            <div className="account">
              <span className="account-mark" aria-hidden="true">
                {Array.from(user.name)[0]}
              </span>
              <span className="account-name">{user.name}</span>
              <button type="button" className="text-btn" onClick={() => void logout()}>
                로그아웃
              </button>
            </div>
          ) : (
            <p className="made-by">LIKE SNAKE 팀이 만들었어요</p>
          )}
        </div>
      </aside>

      <div className="main">
        <header className="head">
          <div className="service only-wide">
            <h1 className="service-name">{text.title}</h1>
          </div>
          <Brand className="only-narrow" />
          <div className="head-actions">
            <p className="source only-wide">KOSIS 데이터 기반</p>
            {canMakeReport && (
              <button type="button" className="btn quiet head-btn" onClick={() => setSheet({ kind: "report" })}>
                보고서
              </button>
            )}
            <button type="button" className="btn quiet head-btn only-narrow in-chat-only" onClick={reset}>
              새 대화
            </button>
            {saved && (
              <button
                type="button"
                className="btn quiet head-btn only-narrow"
                aria-label="지난 대화 보기"
                onClick={() => {
                  setSheet({ kind: "history" });
                  void refreshHistory();
                }}
              >
                기록
              </button>
            )}
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

        <main className="log" aria-live="polite" ref={logRef}>
          {!inChat && (
            <section className="welcome">
              <Mascot place="welcome" className="emblem" label="LIKE SNAKE 캐릭터" />
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
                  {MAIN_EXAMPLES.map((q, i) => (
                    <button type="button" className="example" key={q} onClick={() => void send(q)}>
                      <span className="example-cat">{EXAMPLE_CATEGORIES[i]}</span>
                      <span className="example-q">{q}</span>
                    </button>
                  ))}
                </div>
              </div>
              <button type="button" className="text-btn more-examples" onClick={() => setSheet({ kind: "examples" })}>
                {`다른 질문 예시 ${MORE_EXAMPLES.length}개`}
                <Icon name="chevronDown" size={14} />
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
                  <MascotFace />
                </span>
                <div className="reply">
                  {m.kind === "error" ? (
                    <div className="bubble">{m.text}</div>
                  ) : (
                    <Answer
                      data={m.data}
                      busy={busy}
                      onReport={canMakeReport ? () => setSheet({ kind: "report" }) : undefined}
                      onShowReport={(report) => setSheet({ kind: "report-view", report })}
                      onChartViewChange={(chartView) => changeChartView(m.id, chartView)}
                      onSend={(text, selections) => void send(text, selections)}
                      onOthers={(recs, parsed) => setSheet({ kind: "others", recs, parsed })}
                      onAskAgain={() => inputRef.current?.focus()}
                    />
                  )}
                </div>
              </div>
            ),
          )}

          {(busy || opening) && (
            <div className="msg bot typing">
              <span className="avatar" aria-hidden="true">
                <MascotFace />
              </span>
              <div className="reply">
                <div className="bubble">
                  {opening ? "지난 대화를 불러오고 있어요" : text.typing}
                  <span className="dots" />
                </div>
              </div>
            </div>
          )}
        </main>

        <form className="composer" onSubmit={submit}>
          <div className="composer-box">
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
            <button type="submit" className="send" aria-label="보내기" disabled={busy || opening}>
              <Icon name="arrowUp" size={20} stroke={1.75} />
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
          {MORE_EXAMPLES.map((q) => (
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
      {sheet?.kind === "report" && (
        <ReportSheet sessionId={routeId} answers={answers} onClose={() => setSheet(null)} />
      )}
      {sheet?.kind === "report-view" && (
        <Sheet title="보고서 미리보기" doc onClose={() => setSheet(null)}>
          <ReportPreview report={sheet.report} onBack={() => setSheet(null)} />
        </Sheet>
      )}
      {sheet?.kind === "history" && (
        <Sheet title="지난 대화" narrow onClose={() => setSheet(null)}>
          <History items={history} current={routeId} onOpen={openFromList} onDelete={(id) => void remove(id)} />
        </Sheet>
      )}
    </>
  );
}
