// 계정: 서버 로그인 API(/auth/*) 호출, 로그인 상태, 계정 화면 흐름 (로그인·회원가입·가입 완료)
//
// 토큰 보관
//   Access Token   이 파일의 변수(메모리)에만 둔다. localStorage 에 두면 XSS 로 읽어 갈 수 있다
//   Refresh Token  서버가 HttpOnly 쿠키로 준다. 화면 코드는 읽을 수 없고 /auth 요청에 브라우저가 붙인다
// 화면을 새로 열면 메모리가 비므로 POST /auth/refresh 로 Access Token 을 다시 받는다.
// 브라우저에는 표시 이름만 남긴다: 로그인한 적이 있는지 알아 refresh 를 부를지 정하고,
// 머리글을 바로 그리려는 것이다. 진짜 로그인 확인은 요청마다 서버가 한다.
//
// 파일로 직접 열면(file://) 서버가 없으므로 화면 흐름만 보여 준다 (chat.js 의 목업 모드와 같다).
(function () {
  const KEY = "six.user";
  const DEMO = location.protocol === "file:";
  const OFFLINE = "서버에 연결하지 못했어요. 잠시 뒤에 다시 시도해 주세요.";

  let token = null;
  let refreshing = null; // 진행 중인 refresh. 요청 여러 개가 함께 401 을 받아도 한 번만 보낸다
  const listeners = [];

  function cached() {
    try { return JSON.parse(localStorage.getItem(KEY)); } catch { return null; }
  }
  function remember(user) {
    try {
      if (user) localStorage.setItem(KEY, JSON.stringify(user));
      else localStorage.removeItem(KEY);
      sessionStorage.removeItem(KEY); // 예전 버전이 남긴 값
    } catch { /* 저장 못 해도 화면은 동작 */ }
    listeners.forEach((fn) => fn());
  }
  function forget() {
    token = null;
    remember(null);
  }

  // 서버 오류 → { message, fields: { 칸 이름: 문구 } }
  // FastAPI 는 detail 을 글자(직접 낸 오류) 또는 목록(입력값 검사 422)으로 준다
  async function problem(res) {
    let detail;
    try { detail = (await res.json()).detail; } catch { /* 본문 없음 */ }
    const fields = {};
    if (Array.isArray(detail)) {
      detail.forEach((d) => { fields[d.loc[d.loc.length - 1]] = d.msg; });
      detail = "";
    }
    return {
      status: res.status,
      message: detail || (res.status >= 500 ? OFFLINE : "요청을 처리하지 못했어요."),
      fields,
      wait: Number(res.headers.get("Retry-After")) || 0,
    };
  }

  // POST → { ok, data } 또는 { ok: false, status, message, fields, wait }
  async function post(url, body, form) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: form ? {} : { "Content-Type": "application/json" },
        body: form ? new URLSearchParams(body) : JSON.stringify(body),
      });
      if (!res.ok) return { ok: false, ...(await problem(res)) };
      return { ok: true, data: await res.json() };
    } catch {
      return { ok: false, status: 0, message: OFFLINE, fields: {}, wait: 0 };
    }
  }

  function refresh() {
    if (!refreshing) {
      refreshing = (async () => {
        try {
          const res = await fetch("/auth/refresh", { method: "POST" });
          if (res.ok) {
            token = (await res.json()).access_token;
            return true;
          }
          if (res.status === 401) forget(); // 만료·로그아웃·끊김. 서버 오류(5xx)면 그대로 둔다
          return false;
        } catch {
          return false;
        } finally {
          refreshing = null;
        }
      })();
    }
    return refreshing;
  }

  function withToken(options) {
    const headers = { ...(options.headers || {}) };
    if (token) headers.Authorization = `Bearer ${token}`;
    return { ...options, headers };
  }

  // 로그인했으면 Access Token 을 붙여 부른다. 만료(401)면 한 번만 새로 받아 다시 부르고,
  // 새로 받지 못하면 로그인 없이(손님으로) 다시 부른다
  async function call(url, options = {}) {
    let res = await fetch(url, withToken(options));
    if (res.status === 401 && token) {
      token = null;
      await refresh();
      res = await fetch(url, withToken(options));
    }
    return res;
  }
  // 화면을 열 때의 로그인 확인(ready)이 끝난 뒤에 부른다
  async function authFetch(url, options) {
    await ready;
    return call(url, options);
  }

  async function loadMe() {
    const res = await call("/auth/me");
    if (!res.ok) return false;
    const me = await res.json();
    remember({ name: me.nickname || me.email.split("@")[0] });
    return true;
  }

  // 화면을 열 때: 로그인한 적이 있으면 Access Token 을 다시 받는다
  const ready = (async () => {
    if (DEMO || !cached()) return;
    if (await refresh()) await loadMe();
  })();

  async function login(email, password, keep) {
    if (DEMO) {
      remember({ name: "SIX 멤버" });
      return { ok: true };
    }
    const r = await post("/auth/token", { username: email, password, keep: String(keep) }, true);
    if (!r.ok) return r;
    token = r.data.access_token;
    if (!(await loadMe())) remember({ name: email.split("@")[0] });
    return { ok: true };
  }

  async function logout() {
    if (!DEMO) {
      // Access Token 이 없어도 쿠키만으로 서버의 로그인을 끊는다
      try { await fetch("/auth/logout", withToken({ method: "POST" })); } catch { /* 화면은 로그아웃 */ }
    }
    forget();
  }

  window.SixAccount = {
    ready,
    get: cached, // { name } 또는 null
    fetch: authFetch,
    logout,
    onChange(fn) { listeners.push(fn); },
  };

  const views = document.querySelectorAll(".view");
  if (!views.length) return; // 채팅 화면에서는 위의 도우미만 쓴다

  // ---- 계정 화면 -------------------------------------------------------------------
  function show() {
    const id = location.hash.slice(1);
    const current = document.getElementById(id) || document.getElementById("login");
    views.forEach((v) => { v.hidden = v !== current; });
    document.title = `${current.dataset.title} · TEAM SIX`;
    window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", show);
  show();

  document.querySelectorAll(".eye").forEach((b) => {
    b.addEventListener("click", () => {
      const field = b.parentElement.querySelector("input");
      const showing = field.type === "text";
      field.type = showing ? "password" : "text";
      b.setAttribute("aria-pressed", String(!showing));
      b.setAttribute("aria-label", showing ? "비밀번호 보기" : "비밀번호 숨기기");
    });
  });

  // 칸 아래 안내·오류 문구. kind: "bad"(오류) / "ok" / ""(안내). text 가 없으면 처음 안내로 돌린다
  function note(input, text, kind) {
    const field = input.closest(".field");
    const msg = field.querySelector(".field-msg");
    msg.textContent = text || msg.dataset.hint || "";
    msg.hidden = !msg.textContent;
    msg.className = `field-msg ${text ? kind || "" : ""}`.trim();
    field.classList.toggle("invalid", Boolean(text) && kind === "bad");
  }
  function alertForm(form, text) {
    const msg = form.querySelector(".form-msg");
    msg.textContent = text || "";
    msg.hidden = !text;
  }
  function busy(button, on) {
    button.disabled = on;
    button.setAttribute("aria-busy", String(on));
  }

  // ---- 로그인 ----------------------------------------------------------------------
  const loginForm = document.getElementById("login");
  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { email, password, keep } = loginForm.elements;
    const submit = loginForm.querySelector("[type=submit]");
    alertForm(loginForm, "");
    busy(submit, true);
    const r = await login(email.value.trim(), password.value, keep.checked);
    busy(submit, false);
    if (!r.ok) {
      alertForm(loginForm, r.message);
      password.select();
      return;
    }
    loginForm.reset();
    location.href = "index.html";
  });

  // ---- 회원가입: ① 인증번호 받기 → ② 인증번호 확인 → ③ 가입 -------------------------
  const signup = document.getElementById("signup");
  const f = signup.elements;
  const sendBtn = document.getElementById("code-send");
  const verifyBtn = document.getElementById("code-verify");
  const codeField = f.code.closest(".field");
  const PASSWORD_RULE = "6~20자, 영문·숫자·특수기호를 모두 넣어 주세요.";

  let verified = null; // 인증을 마친 이메일
  let cooldown = null; // 다시 받기까지 남은 시간을 세는 타이머

  const emailNow = () => f.email.value.trim().toLowerCase();

  function stopCooldown() {
    clearInterval(cooldown);
    cooldown = null;
  }
  function startCooldown(seconds) {
    stopCooldown();
    let left = seconds;
    const tick = () => {
      sendBtn.disabled = left > 0;
      sendBtn.textContent = left > 0 ? `다시 받기 (${left}초)` : "다시 받기";
      if (left <= 0) stopCooldown();
      left -= 1;
    };
    tick();
    cooldown = setInterval(tick, 1000);
  }
  function resetVerify() {
    verified = null;
    stopCooldown();
    sendBtn.disabled = false;
    sendBtn.textContent = "인증번호 받기";
    codeField.hidden = true;
    f.code.value = "";
    note(f.code, "");
  }

  // 이메일을 고치면 인증을 처음부터 다시 한다
  f.email.addEventListener("input", () => {
    if (verified || !codeField.hidden || cooldown) resetVerify();
    note(f.email, "");
  });

  sendBtn.addEventListener("click", async () => {
    if (!f.email.checkValidity()) {
      f.email.reportValidity();
      return;
    }
    if (DEMO) {
      verified = emailNow();
      sendBtn.disabled = true;
      sendBtn.textContent = "인증 완료";
      return;
    }
    busy(sendBtn, true);
    const r = await post("/auth/email-code", { email: emailNow() });
    busy(sendBtn, false);
    if (!r.ok) {
      note(f.email, r.status === 422 ? "이메일 주소를 다시 확인해 주세요." : r.message, "bad");
      if (r.status === 429 && r.wait) startCooldown(r.wait);
      return;
    }
    const minutes = Math.round(r.data.expires_in / 60);
    note(f.email, `인증번호를 메일로 보냈어요. ${minutes}분 안에 입력해 주세요.`, "ok");
    codeField.hidden = false;
    f.code.value = "";
    note(f.code, "");
    f.code.focus();
    startCooldown(r.data.resend_after);
  });

  async function verify() {
    const code = f.code.value.trim();
    if (!/^\d{6}$/.test(code)) {
      note(f.code, "메일로 받은 6자리 숫자를 입력해 주세요.", "bad");
      return;
    }
    const email = emailNow();
    busy(verifyBtn, true);
    const r = await post("/auth/email-code/verify", { email, code });
    busy(verifyBtn, false);
    if (!r.ok) {
      note(f.code, r.fields.code || r.message, "bad");
      f.code.select();
      return;
    }
    resetVerify();
    verified = email;
    sendBtn.disabled = true;
    sendBtn.textContent = "인증 완료";
    note(f.email, "이메일 인증을 마쳤어요.", "ok");
    f.password.focus();
  }
  verifyBtn.addEventListener("click", verify);
  f.code.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault(); // 가입 폼이 제출되지 않게
    verify();
  });
  f.code.addEventListener("input", () => note(f.code, ""));

  function passwordProblem(v) {
    const ok = v.length >= 6 && v.length <= 20
      && /[A-Za-z]/.test(v) && /[0-9]/.test(v) && /[^A-Za-z0-9\s]/.test(v);
    return ok ? "" : PASSWORD_RULE;
  }
  function checkConfirm() {
    const same = f.confirm.value === f.password.value;
    f.confirm.setCustomValidity(same ? "" : "비밀번호가 서로 달라요. 다시 확인해 주세요.");
  }
  f.password.addEventListener("input", () => {
    note(f.password, "");
    checkConfirm();
  });
  f.confirm.addEventListener("input", checkConfirm);
  f.nickname.addEventListener("input", () => note(f.nickname, ""));

  signup.addEventListener("submit", async (e) => {
    e.preventDefault();
    alertForm(signup, "");
    if (verified !== emailNow()) {
      note(f.email, "이메일 인증을 먼저 해 주세요.", "bad");
      f.email.focus();
      return;
    }
    const weak = passwordProblem(f.password.value);
    if (weak) {
      note(f.password, weak, "bad");
      f.password.focus();
      return;
    }
    const submit = signup.querySelector("[type=submit]");
    if (!DEMO) {
      busy(submit, true);
      const r = await post("/auth/register", {
        email: verified,
        password: f.password.value,
        nickname: f.nickname.value,
      });
      busy(submit, false);
      if (!r.ok) {
        ["nickname", "email", "password"].forEach((name) => {
          if (r.fields[name]) note(f[name], r.fields[name], "bad");
        });
        if (r.status === 400 || r.status === 409) {
          // 400: 인증한 지 30분이 지남 → 다시 인증. 409: 그사이 누가 같은 이메일로 가입함
          resetVerify();
          note(f.email, r.message, "bad");
        } else if (!Object.keys(r.fields).length) {
          alertForm(signup, r.message);
        }
        return;
      }
    }
    signup.reset();
    resetVerify();
    [f.nickname, f.email, f.password].forEach((input) => note(input, ""));
    location.hash = "done";
  });
})();
