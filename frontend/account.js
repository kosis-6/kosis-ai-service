// 계정 화면 흐름 (로그인·회원가입·가입 완료)
// 서버에 인증 API가 아직 없어 화면 흐름만 보여 주는 단계다: 입력값을 어디에도 보내지 않고,
// 비밀번호·이메일은 저장하지 않는다. 브라우저에는 표시 이름만 남긴다.
(function () {
  const KEY = "six.user";

  function read(store) {
    try { return JSON.parse(store.getItem(KEY)); } catch { return null; }
  }

  window.SixAccount = {
    get() {
      return read(localStorage) || read(sessionStorage);
    },
    set(user, keep) {
      this.clear();
      try { (keep ? localStorage : sessionStorage).setItem(KEY, JSON.stringify(user)); } catch { /* 저장 못 해도 화면은 동작 */ }
    },
    clear() {
      try { localStorage.removeItem(KEY); sessionStorage.removeItem(KEY); } catch { /* 저장 못 해도 화면은 동작 */ }
    },
  };

  const views = document.querySelectorAll(".view");
  if (!views.length) return; // 채팅 화면에서는 저장 도우미만 쓴다

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

  const login = document.getElementById("login");
  login.addEventListener("submit", (e) => {
    e.preventDefault();
    window.SixAccount.set({ name: "SIX 멤버" }, login.elements.keep.checked);
    login.reset();
    location.href = "index.html";
  });

  const signup = document.getElementById("signup");
  const confirm = signup.elements.confirm;
  function checkConfirm() {
    confirm.setCustomValidity(confirm.value === signup.elements.password.value ? "" : "비밀번호가 서로 달라요. 다시 확인해 주세요.");
  }
  signup.elements.password.addEventListener("input", checkConfirm);
  confirm.addEventListener("input", checkConfirm);
  signup.addEventListener("submit", (e) => {
    e.preventDefault();
    signup.reset();
    location.hash = "done";
  });
})();
