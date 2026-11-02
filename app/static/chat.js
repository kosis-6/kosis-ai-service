// 통계표 안내 채팅 화면: POST /search 응답(status별)을 말풍선·표 카드·선택 버튼으로 그린다
// 목업 모드(?mock=1 또는 파일로 직접 열었을 때)는 mock.js 가 응답을 만들어 서버·HCX 를 부르지 않는다
(function () {
  const $ = (id) => document.getElementById(id);
  const log = $("log");
  const form = $("ask");
  const input = $("question");
  const sendBtn = $("send");
  const mockToggle = $("mock-toggle");
  const debugToggle = $("debug-toggle");
  const badge = $("mode-badge");

  const AXIS_LABELS = { target: "대상", sex: "성별", edition: "연도" };
  const CHECK_LABELS = { year: "연도", region_level: "지역 수준", sex: "성별", target: "대상", projection: "장래 추계" };

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
  mockToggle.checked = params.has("mock")
    ? params.get("mock") !== "0"
    : location.protocol === "file:" || load("chat.mock") === "1";
  debugToggle.checked = params.has("debug") || load("chat.debug") === "1";

  function syncMode() {
    badge.hidden = !mockToggle.checked;
    save("chat.mock", mockToggle.checked ? "1" : "0");
  }
  mockToggle.addEventListener("change", () => { syncMode(); reset(); });
  debugToggle.addEventListener("change", () => save("chat.debug", debugToggle.checked ? "1" : "0"));
  syncMode();

  // ---- 그리기 도우미 --------------------------------------------------------------
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
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

  function hideWelcome() {
    const w = $("welcome");
    if (w) w.remove();
  }

  function addUser(text) {
    hideWelcome();
    const msg = el("div", "msg user");
    msg.append(el("div", "bubble", text));
    log.append(msg);
    scrollDown();
  }

  function addTyping() {
    const msg = el("div", "msg bot typing");
    const b = el("div", "bubble", "통계표를 찾고 있어요");
    b.append(el("span", "dots"));
    msg.append(b);
    log.append(msg);
    scrollDown();
    return msg;
  }

  function addError(text) {
    const msg = el("div", "msg bot error");
    msg.append(el("div", "bubble", text));
    log.append(msg);
    scrollDown();
  }

  // ---- 응답 그리기 ---------------------------------------------------------------
  function checkLabel(key, parsed) {
    const c = (parsed && parsed.conditions) || {};
    if (key === "year" && c.year) return c.year_end ? `${c.year}~${c.year_end}년` : `${c.year}년`;
    if (key === "region_level" && c.region_level) return c.region_level;
    if (key.includes(":")) return key.split(":")[1];
    return CHECK_LABELS[key] || key;
  }

  function card(r, parsed, top) {
    const c = el("article", top ? "card top" : "card");
    c.append(el("div", "rank", top ? "추천 통계표" : `후보 ${r.rank}`));
    c.append(el("h3", null, r.tbl_nm));
    c.append(el("div", "meta", `${r.org_nm} · ${r.survey_nm} · ${r.period}년`));
    if (r.path) c.append(el("div", "path", r.path));

    const checks = Object.entries(r.checks || {});
    if (checks.length) {
      const row = el("div", "row");
      row.append(el("span", "small-label", "조건"));
      for (const [k, ok] of checks) row.append(el("span", `check ${ok ? "ok" : "no"}`, `${checkLabel(k, parsed)} ${ok ? "✔" : "✘"}`));
      c.append(row);
    }

    const fam = r.family || {};
    const versions = [
      ...(fam.members || []).map((m) => m.label),
      ...(fam.editions || []).map((e) => `${e.period}년판`),
    ];
    if (versions.length) {
      const row = el("div", "row");
      row.append(el("span", "small-label", "같은 표의 다른 버전"));
      versions.slice(0, 10).forEach((v) => row.append(el("span", "ver", v)));
      if (versions.length > 10) row.append(el("span", "small-label", `외 ${versions.length - 10}개`));
      c.append(row);
    }

    if (r.notes && r.notes.length) {
      const ul = el("ul");
      r.notes.forEach((n) => ul.append(el("li", null, n)));
      c.append(ul);
    }

    const a = el("a", "link", "KOSIS에서 보기 ↗");
    a.href = r.url;
    a.target = "_blank";
    a.rel = "noopener";
    c.append(a);
    return c;
  }

  function memberChoices(clarify) {
    const box = el("div", "choices");
    const picked = {};
    const go = el("button", "primary", "이렇게 찾기");
    go.type = "button";
    go.disabled = true;

    for (const a of clarify.axes) {
      const axis = el("div", "axis");
      axis.append(el("span", "small-label", AXIS_LABELS[a.axis] || a.axis));
      const chips = el("div", "chips");
      for (const o of a.options) {
        const b = el("button", "chip", a.axis === "edition" ? `${o}년` : o);
        b.type = "button";
        b.addEventListener("click", () => {
          const same = picked[a.axis] === o;
          chips.querySelectorAll(".chip").forEach((x) => x.classList.remove("on"));
          if (same) delete picked[a.axis];
          else { picked[a.axis] = o; b.classList.add("on"); }
          go.disabled = Object.keys(picked).length === 0;
        });
        chips.append(b);
      }
      axis.append(chips);
      box.append(axis);
    }

    const any = el("button", "ghost", "아무거나");
    any.type = "button";
    const actions = el("div", "actions");
    actions.append(go, any);
    box.append(actions);

    go.addEventListener("click", () => {
      // 대상·성별은 selections 로, 판은 서버가 알아듣는 'YYYY~YYYY년판' 문장으로 보낸다
      const parts = [];
      const selections = {};
      for (const axis of ["target", "sex"]) {
        if (picked[axis]) { parts.push(picked[axis]); selections[axis] = picked[axis]; }
      }
      if (picked.edition) parts.push(`${picked.edition}년판`);
      box.classList.add("done");
      send(parts.join(", "), selections);
    });
    any.addEventListener("click", () => { box.classList.add("done"); send("아무거나"); });
    return box;
  }

  function familyChoices(clarify) {
    const box = el("div", "choices");
    box.append(el("span", "small-label", "찾는 자료를 골라 주세요"));
    clarify.families.forEach((f, i) => {
      const b = el("button", "family-btn");
      b.type = "button";
      b.append(`${"①②③④⑤"[i] || i + 1} ${f.tbl_nm}`);
      b.append(el("small", null, `${f.survey_nm} · ${f.period}년`));
      b.addEventListener("click", () => {
        box.classList.add("done");
        send(f.tbl_nm, { family_id: f.family_id });
      });
      box.append(b);
    });
    return box;
  }

  function debugPanel(data) {
    const d = data.debug || {};
    const box = el("details", "debug");
    box.append(el("summary", null, `디버그 · ${data.status} · ${d.retrieval || "-"}${d.mock ? " · 목업" : ""}`));
    const body = el("div", "body");

    const cands = d.candidates || [];
    if (cands.length) {
      const wrap = el("div", "scroll");
      const t = el("table");
      const head = el("tr");
      ["점수", "키워드", "임베딩", "표", "기간", "조건"].forEach((h) => head.append(el("th", null, h)));
      t.append(head);
      for (const c of cands) {
        const tr = el("tr");
        tr.append(el("td", "num", c.score?.toFixed?.(3) ?? "-"));
        tr.append(el("td", "num", c.keyword?.toFixed?.(3) ?? "-"));
        tr.append(el("td", "num", c.dense == null ? "-" : c.dense.toFixed(3)));
        tr.append(el("td", null, `${c.table_name} (${c.tbl_id})`));
        tr.append(el("td", null, c.edition || ""));
        tr.append(el("td", null, Object.entries(c.checks || {}).map(([k, ok]) => `${k}${ok ? "✔" : "✘"}`).join(" ")));
        t.append(tr);
      }
      wrap.append(t);
      body.append(el("div", "small-label", `후보 ${cands.length}개`), wrap);
    }

    body.append(el("div", "small-label", "질의 분석 (parsed)"));
    body.append(el("pre", null, JSON.stringify(data.parsed, null, 2)));
    const rest = { ...d };
    delete rest.candidates;
    body.append(el("div", "small-label", "그 밖의 디버그 값"));
    body.append(el("pre", null, JSON.stringify(rest, null, 2)));
    body.append(el("div", "small-label", `session_id: ${data.session_id}`));

    box.append(body);
    return box;
  }

  function render(data) {
    const msg = el("div", "msg bot");
    if (debugToggle.checked) msg.append(el("span", "status", data.status));

    const recs = data.recommendations || [];
    // 바로가기 줄은 카드의 버튼과 겹쳐서 말풍선에서만 뺀다
    const text = recs.length
      ? data.answer.split("\n").filter((l) => !l.startsWith("바로가기:")).join("\n")
      : data.answer;
    const bubble = el("div", "bubble");
    bubble.append(linkified(text));
    msg.append(bubble);

    if (recs.length) {
      const cards = el("div", "cards");
      cards.append(card(recs[0], data.parsed, true));
      if (recs.length > 1) {
        const more = el("details", "others");
        more.append(el("summary", null, `다른 후보 ${recs.length - 1}개 보기`));
        const list = el("div", "cards");
        recs.slice(1).forEach((r) => list.append(card(r, data.parsed, false)));
        more.append(list);
        cards.append(more);
      }
      msg.append(cards);
    }

    if (data.clarify && data.clarify.level === "member") msg.append(memberChoices(data.clarify));
    if (data.clarify && data.clarify.level === "family") msg.append(familyChoices(data.clarify));

    if (["out_of_scope", "not_statistics", "parse_error"].includes(data.status)) {
      const chips = el("div", "chips");
      chips.style.justifyContent = "flex-start";
      window.MockApi.examples.slice(0, 3).forEach((q) => chips.append(exampleChip(q)));
      msg.append(chips);
    }

    if (debugToggle.checked && data.debug) msg.append(debugPanel(data));
    log.append(msg);
    scrollDown();
  }

  // ---- 보내기 -------------------------------------------------------------------
  async function callSearch(body) {
    if (mockToggle.checked) return window.MockApi.search(body);
    const res = await fetch("/search", {
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
    const body = { question, session_id: sessionId, top_k: 3, debug: debugToggle.checked };
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

  function exampleChip(q) {
    const b = el("button", "chip", q);
    b.type = "button";
    b.addEventListener("click", () => send(q));
    return b;
  }

  function reset() {
    sessionId = null;
    log.replaceChildren();
    const w = el("section", "welcome");
    w.id = "welcome";
    w.append(el("h2", null, "어떤 통계를 찾으세요?"));
    w.append(el("p", null, "평소 말투로 물어보면 맞는 통계표를 찾아 KOSIS 바로가기와 함께 안내해 드려요."));
    const chips = el("div", "chips");
    window.MockApi.examples.forEach((q) => chips.append(exampleChip(q)));
    w.append(chips);
    log.append(w);
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
  $("reset").addEventListener("click", reset);

  reset();
  input.focus();
})();
