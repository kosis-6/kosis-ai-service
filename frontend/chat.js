// 통계표 안내 채팅 화면: POST /search 응답(status별)을 말풍선·표 카드·선택 버튼으로 그린다
// 목업 모드(?mock=1 또는 파일로 직접 열었을 때)는 mock.js 가 응답을 만들어 서버·HCX 를 부르지 않는다
// 개발 도구(목업·디버그 전환, 검색 과정)는 ?mock=1 / ?debug=1 로 켰을 때만 보인다
(function () {
  const $ = (id) => document.getElementById(id);
  const log = $("log");
  const form = $("ask");
  const input = $("question");
  const sendBtn = $("send");
  const devBtn = $("dev-tools");
  const sheet = $("sheet");

  const AXIS_LABELS = { target: "대상", sex: "성별", edition: "연도" };
  const CHECK_LABELS = {
    year: "연도", region_level: "지역 수준", sex: "성별", target: "대상", projection: "장래 추계",
    spouse: "배우자", exclude: "제외 조건",
  };
  const EXAMPLE_CATEGORIES = ["가구와 반려동물", "출생과 인구", "국적과 주민", "지역과 외국인"];
  const NO_RESULT_TITLES = {
    out_of_scope: "제공 범위 밖의 질문",
    not_statistics: "통계 질문 안내",
    parse_error: "다시 질문해 주세요",
  };
  // 카드·알림으로 따로 보여 주는 줄은 말풍선에서 뺀다
  const CARD_LINES = ["추천 통계표:", "조건 확인:", "확인 필요:", "같은 표의 다른 버전:", "바로가기:"];

  let sessionId = null;
  let busy = false;

  // ---- 설정 (보기 편의용이라 저장 실패해도 동작) -----------------------------------
  function load(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }
  function save(key, value) {
    try { localStorage.setItem(key, value); } catch { /* 저장 못 해도 화면은 동작 */ }
  }

  const params = new URLSearchParams(location.search);
  let mockOn = params.has("mock")
    ? params.get("mock") !== "0"
    : location.protocol === "file:" || load("chat.mock") === "1";
  let debugOn = params.has("debug") ? params.get("debug") !== "0" : load("chat.debug") === "1";

  function syncMode() {
    save("chat.mock", mockOn ? "1" : "0");
    save("chat.debug", debugOn ? "1" : "0");
    devBtn.hidden = !mockOn && !debugOn;
    devBtn.textContent = mockOn ? "목업 모드" : "개발 도구";
  }

  // ---- 그리기 도우미 --------------------------------------------------------------
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function button(cls, text, onClick) {
    const b = el("button", cls, text);
    b.type = "button";
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }

  function kosisLink(url, cls) {
    const a = el("a", `btn primary ${cls || ""}`, "KOSIS에서 보기  ↗");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    return a;
  }

  // 텍스트 안의 주소만 링크로 바꾼다 (나머지는 그대로 글자)
  function linkified(text) {
    const frag = document.createDocumentFragment();
    const re = /https?:\/\/[^\s)]+/g;
    let last = 0;
    for (const m of text.matchAll(re)) {
      frag.append(text.slice(last, m.index));
      const a = el("a", null, m[0]);
      a.href = m[0];
      a.target = "_blank";
      a.rel = "noopener";
      frag.append(a);
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    return frag;
  }

  function scrollDown() {
    log.scrollTop = log.scrollHeight;
  }

  function botMessage(extraClass) {
    const msg = el("div", `msg bot ${extraClass || ""}`);
    msg.append(Object.assign(el("span", "avatar", "6"), { ariaHidden: "true" }));
    const reply = el("div", "reply");
    msg.append(reply);
    log.append(msg);
    return reply;
  }

  function addUser(text) {
    document.body.classList.add("in-chat");
    log.querySelector(".welcome")?.remove();
    const msg = el("div", "msg user");
    msg.append(el("div", "bubble", text));
    log.append(msg);
    scrollDown();
  }

  function addTyping() {
    const reply = botMessage("typing");
    const b = el("div", "bubble", "통계표를 찾고 있어요");
    b.append(el("span", "dots"));
    reply.append(b);
    scrollDown();
    return reply.parentElement;
  }

  function addError(text) {
    botMessage("error").append(el("div", "bubble", text));
    scrollDown();
  }

  // ---- 추가 상태 패널 -------------------------------------------------------------
  function openSheet(title, children, narrow) {
    $("sheet-title").textContent = title;
    $("sheet-body").replaceChildren(...children);
    sheet.classList.toggle("narrow", Boolean(narrow));
    if (!sheet.open) sheet.showModal();
  }

  function openOthers(recs, parsed) {
    openSheet("다른 후보", [
      el("p", "sheet-text", "같은 주제의 다른 통계표도 확인해 보세요."),
      ...recs.map((r) => altCard(r, parsed)),
    ]);
  }

  function openMoreExamples(examples) {
    openSheet("다른 질문 예시", examples.map((q) => button("btn secondary", q, () => {
      sheet.close();
      send(q);
    })), true);
  }

  function openDevTools(data) {
    const options = el("div", "dev-options");
    const toggle = (label, on, change) => {
      const b = button("dev-toggle", `${label}  ${on ? "● 켜짐" : "○ 꺼짐"}`, change);
      b.setAttribute("aria-pressed", String(on));
      return b;
    };
    options.append(
      toggle("목업 모드", mockOn, () => { mockOn = !mockOn; syncMode(); sheet.close(); reset(); }),
      toggle("디버그", debugOn, () => { debugOn = !debugOn; syncMode(); openDevTools(data); }),
    );

    const children = [options];
    if (mockOn) children.push(el("p", "dev-note", "화면 개발용 목업 결과입니다. 통계 수치를 생성하지 않습니다."));
    if (data && data.debug) {
      const d = data.debug;
      const p = data.parsed || {};
      const cands = d.candidates || [];
      children.push(el("div", "dev-summary", [
        `status: ${data.status}`,
        `retrieval: ${d.retrieval || "-"}`,
        `query_type: ${p.query_type || "-"}`,
        `topic: ${p.topic || "-"}`,
        `후보 수: ${cands.length}`,
      ].join("\n")));
      if (cands.length) {
        children.push(el("p", "dev-heading", "후보 통계표"));
        for (const c of cands) {
          const score = c.score == null ? "" : ` · ${c.score.toFixed(3)}`;
          children.push(el("p", "dev-cand", `${c.tbl_id} · ${c.table_name}${score}`));
        }
      }
      const raw = el("details", "dev-raw");
      raw.append(el("summary", null, "원본 값 보기"));
      raw.append(el("pre", null, JSON.stringify({ session_id: data.session_id, parsed: data.parsed, debug: d }, null, 2)));
      children.push(raw);
    }
    openSheet("개발 도구", children);
  }

  // ---- 응답 그리기 ---------------------------------------------------------------
  function checkLabel(key, parsed) {
    const c = (parsed && parsed.conditions) || {};
    if (key === "year" && c.year) return c.year_end ? `${c.year}~${c.year_end}년` : `${c.year}년`;
    if (key === "region_level" && c.region_level) return c.region_level;
    if (key.includes(":")) return key.split(":")[1];
    return CHECK_LABELS[key] || key;
  }

  function checkPills(r, parsed) {
    return Object.entries(r.checks || {}).map(([k, ok]) => (
      el("span", ok ? "pill on" : "pill", `${checkLabel(k, parsed)}  ${ok ? "✓" : "✕"}`)
    ));
  }

  function metaLine(r) {
    return `${r.org_nm}  ·  ${r.survey_nm}  ·  ${r.period}년`;
  }

  // 경로 끝이 표 이름이면 카드 제목과 겹치므로 뺀다
  function pathText(r) {
    if (!r.path) return null;
    const parts = r.path.split(" > ");
    if (parts[parts.length - 1] === r.tbl_nm) parts.pop();
    return parts.join("  ›  ");
  }

  // 시안처럼 미리 골라 둘 값: 판은 최신(첫 번째), 대상·성별은 합계를 뜻하는 값이 있을 때만
  function defaultOption(a) {
    if (a.axis === "edition") return a.options[0];
    return a.options.find((o) => o === "전체" || o === "계");
  }

  function otherVersions(fam) {
    const versions = [
      ...(fam.members || []).map((m) => m.label),
      ...(fam.editions || []).map((e) => `${e.period}년판`),
    ];
    if (!versions.length) return null;
    const shown = versions.slice(0, 10).join(", ");
    const rest = versions.length > 10 ? ` 외 ${versions.length - 10}개` : "";
    return `같은 표의 다른 버전: ${shown}${rest}`;
  }

  function topCard(r, parsed) {
    const c = el("article", "card");
    c.append(el("span", "card-badge", "추천 통계표"));
    c.append(el("h3", null, r.tbl_nm));
    c.append(el("p", "card-meta", metaLine(r)));
    const path = pathText(r);
    if (path) c.append(el("p", "card-path", path));
    const pills = el("div", "pills");
    pills.append(el("span", "pill on", `제공 기간  ${r.period}년`), ...checkPills(r, parsed));
    c.append(pills);
    const others = otherVersions(r.family || {});
    if (others) c.append(el("p", "card-versions", others));
    c.append(kosisLink(r.url, "card-link"));
    return c;
  }

  function altCard(r, parsed) {
    const c = el("article", "alt-card");
    c.append(el("h3", null, r.tbl_nm));
    c.append(el("p", "card-meta", metaLine(r).replaceAll("  ·  ", " · ")));
    const checks = checkPills(r, parsed);
    if (checks.length) {
      const pills = el("div", "pills");
      pills.append(...checks);
      c.append(pills);
    }
    if (r.notes && r.notes.length) {
      const ul = el("ul", "card-notes");
      r.notes.forEach((n) => ul.append(el("li", null, n)));
      c.append(ul);
    }
    c.append(kosisLink(r.url));
    return c;
  }

  function memberChoices(clarify) {
    const box = el("div", "choices");
    const picked = {};
    const go = button("btn primary", "이렇게 찾기");

    for (const a of clarify.axes) {
      const axis = el("div", "axis");
      axis.append(el("span", "axis-label", AXIS_LABELS[a.axis] || a.axis));
      const pills = el("div", "pills");
      const preset = defaultOption(a);
      a.options.forEach((o) => {
        const b = button("pill", a.axis === "edition" ? `${o}년` : o, () => {
          const same = picked[a.axis] === o;
          pills.querySelectorAll(".pill").forEach((x) => {
            x.classList.remove("on");
            x.setAttribute("aria-pressed", "false");
          });
          if (same) delete picked[a.axis];
          else {
            picked[a.axis] = o;
            b.classList.add("on");
            b.setAttribute("aria-pressed", "true");
          }
          go.disabled = Object.keys(picked).length === 0;
        });
        const on = o === preset;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", String(on));
        if (on) picked[a.axis] = o;
        pills.append(b);
      });
      axis.append(pills);
      box.append(axis);
    }
    go.disabled = Object.keys(picked).length === 0;

    const any = button("btn quiet", "아무거나", () => {
      if (busy) return;
      box.inert = true;
      send("아무거나");
    });
    go.addEventListener("click", () => {
      if (busy) return;
      // 대상·성별은 selections 로, 판은 서버가 알아듣는 'YYYY~YYYY년판' 문장으로 보낸다
      const parts = [];
      const selections = {};
      for (const axis of ["target", "sex"]) {
        if (picked[axis]) { parts.push(picked[axis]); selections[axis] = picked[axis]; }
      }
      if (picked.edition) parts.push(`${picked.edition}년판`);
      box.inert = true;
      send(parts.join(", "), selections);
    });
    const actions = el("div", "actions");
    actions.append(go, any);
    box.append(actions);
    return box;
  }

  function familyChoices(clarify) {
    const box = el("div", "choices");
    box.append(el("span", "axis-label", "찾는 자료를 골라 주세요"));
    const list = el("div", "family-list");
    clarify.families.forEach((f, i) => {
      const b = button("family-btn", null, () => {
        if (busy) return;
        box.inert = true;
        send(f.tbl_nm, { family_id: f.family_id });
      });
      b.append(`${"①②③④⑤"[i] || i + 1} ${f.tbl_nm}`);
      b.append(el("small", null, `${f.survey_nm} · ${f.period}년`));
      list.append(b);
    });
    box.append(list);
    return box;
  }

  function noticePanel(data) {
    const box = el("div", "notice");
    box.append(el("p", "notice-title", data.status === "partial_match"
      ? "질문의 조건을 모두 충족하는 표를 찾지 못했어요."
      : "확인이 필요해요"));
    const lines = data.notice.split("; ");
    if (data.status === "partial_match") lines.push("표의 제공 기간과 조건을 확인해 주세요.");
    box.append(el("p", "notice-text", lines.join("\n")));
    return box;
  }

  // 서버 안내 문장 중 말풍선에 둘 줄만 고른다 (표 정보·선택지는 카드와 버튼으로 보여 준다)
  function bubbleText(data) {
    const lines = data.answer.split("\n");
    if (data.clarify) return lines[0];
    if (!(data.recommendations || []).length) return data.answer;
    const kept = lines
      .filter((l) => !CARD_LINES.some((p) => l.startsWith(p)))
      .map((l) => l.replace(/^참고: /, ""));
    if (kept.length === 1) kept.push("아래 통계표에서 자세한 자료를 살펴보세요.");
    return kept.join("\n");
  }

  function render(data) {
    const reply = botMessage();
    const recs = data.recommendations || [];

    const bubble = el("div", "bubble");
    const title = NO_RESULT_TITLES[data.status];
    if (title) bubble.append(el("p", "bubble-title", title));
    bubble.append(linkified(bubbleText(data)));
    reply.append(bubble);

    if (data.notice) reply.append(noticePanel(data));
    if (recs.length) reply.append(topCard(recs[0], data.parsed));
    if (recs.length > 1) {
      reply.append(button("btn secondary card-link", `다른 후보 ${recs.length - 1}개 보기  ⌄`, () => {
        openOthers(recs.slice(1), data.parsed);
      }));
    }

    if (data.clarify && data.clarify.level === "member") reply.append(memberChoices(data.clarify));
    if (data.clarify && data.clarify.level === "family") reply.append(familyChoices(data.clarify));

    if (title) {
      reply.append(button("btn primary ask-again", "다른 통계 질문하기", () => input.focus()));
    }
    if (data.debug) {
      reply.append(button("text-btn debug-link", "디버그 · 검색 과정 보기  ›", () => openDevTools(data)));
    }
    scrollDown();
  }

  // ---- 보내기 -------------------------------------------------------------------
  async function callSearch(body) {
    if (mockOn) return window.MockApi.search(body);
    // 로그인했으면 토큰을 붙여 보낸다 (검색 기록이 사용자와 이어진다). 만료되면 새로 받아 다시 보낸다
    const res = await window.SixAccount.fetch("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = "";
      try { detail = JSON.stringify((await res.json()).detail); } catch { /* 본문 없음 */ }
      throw new Error(`서버 오류 ${res.status}${detail ? `: ${detail}` : ""}`);
    }
    return res.json();
  }

  async function send(question, selections) {
    question = question.trim();
    if (!question || busy) return;
    busy = true;
    sendBtn.disabled = true;
    addUser(question);
    const typing = addTyping();
    const body = { question, session_id: sessionId, top_k: 3, debug: debugOn };
    if (selections && Object.keys(selections).length) body.selections = selections;
    try {
      const data = await callSearch(body);
      sessionId = data.session_id;
      typing.remove();
      render(data);
    } catch (e) {
      typing.remove();
      addError(`답변을 가져오지 못했어요. ${e.message || e}`);
    } finally {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  function exampleCard(q, category) {
    const b = button("example", null, () => send(q));
    b.append(el("span", "example-cat", `${category}  ↗`), el("span", "example-q", q));
    return b;
  }

  function reset() {
    sessionId = null;
    document.body.classList.remove("in-chat");
    const welcome = $("welcome-template").content.cloneNode(true);
    const examples = window.MockApi.examples;
    const main = examples.slice(0, EXAMPLE_CATEGORIES.length);
    const more = examples.slice(EXAMPLE_CATEGORIES.length);
    welcome.querySelector(".examples-grid").append(...main.map((q, i) => exampleCard(q, EXAMPLE_CATEGORIES[i])));
    const moreBtn = welcome.querySelector(".more-examples");
    moreBtn.textContent = `다른 질문 예시  ${more.length}  ⌄`;
    moreBtn.addEventListener("click", () => openMoreExamples(more));
    log.replaceChildren(welcome);
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value;
    input.value = "";
    input.style.height = "";
    send(q);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  });
  document.querySelectorAll(".js-reset").forEach((b) => b.addEventListener("click", reset));
  devBtn.addEventListener("click", () => openDevTools(null));
  $("sheet-close").addEventListener("click", () => sheet.close());
  // 패널 바깥(배경)을 누르면 닫는다
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.close(); });

  // 로그인 상태에 맞는 머리글·사이드바를 보여 준다
  function syncAccount() {
    const user = window.SixAccount.get();
    document.querySelectorAll("[data-auth]").forEach((node) => {
      node.hidden = (node.dataset.auth === "in") !== Boolean(user);
    });
    document.querySelectorAll(".js-name").forEach((node) => { node.textContent = user ? user.name : ""; });
  }
  document.querySelectorAll(".js-logout").forEach((b) => b.addEventListener("click", () => {
    window.SixAccount.logout();
  }));
  // 로그인 확인이 끝나거나(화면을 열 때), 로그아웃·만료로 상태가 바뀌면 다시 그린다
  window.SixAccount.onChange(syncAccount);

  syncAccount();
  syncMode();
  reset();
  input.focus();
})();
