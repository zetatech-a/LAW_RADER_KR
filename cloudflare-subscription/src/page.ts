// 설정 페이지 정적 자산. 인라인 스크립트·스타일이 없어 CSP 'self' 만으로 동작한다.
// 기관 목록은 하드코딩하지 않고 로그인 후 /api/preferences 의 catalog(D1)로 그린다.
// 서버 데이터는 textContent 로만 화면에 넣는다(innerHTML 미사용).

export const INDEX_HTML = `<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>LAW RADER KR · 수신 기관 설정</title>
<link rel="stylesheet" href="/app.css">
<script src="/app.js" defer></script>
</head>
<body>
<main class="card" aria-labelledby="title">
  <header>
    <p class="brand">LAW RADER KR</p>
    <h1 id="title">수신 기관 설정</h1>
    <p class="lead">기존 구독자만 이용할 수 있습니다. 구독 중인 이메일로 인증번호를 받아 확인해 주세요.</p>
  </header>

  <p id="status" class="status" role="status" aria-live="polite"></p>

  <section id="step-email" hidden>
    <form id="form-email" novalidate>
      <label for="email">구독 이메일</label>
      <input id="email" name="email" type="email" autocomplete="email" inputmode="email" required maxlength="254">
      <p class="hint">등록된 구독자에게만 인증번호가 발송됩니다. 메일이 오지 않으면 주소를 확인해 주세요.</p>
      <button type="submit" class="primary">인증번호 받기</button>
    </form>
  </section>

  <section id="step-code" hidden>
    <form id="form-code" novalidate>
      <label for="code">인증번호 6자리</label>
      <input id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required>
      <p class="hint">인증번호는 10분간 유효하며 5회까지 입력할 수 있습니다.</p>
      <div class="row">
        <button type="submit" class="primary">확인</button>
        <button type="button" id="resend" class="ghost">번호 다시 받기</button>
        <button type="button" id="change-email" class="ghost">이메일 변경</button>
      </div>
    </form>
  </section>

  <section id="step-prefs" hidden>
    <form id="form-prefs" novalidate>
      <fieldset>
        <legend>받을 알림</legend>
        <label class="choice"><input type="radio" name="mode" value="all"> <span>모든 기관 수신</span></label>
        <label class="choice"><input type="radio" name="mode" value="selected"> <span>선택한 기관만 수신</span></label>
      </fieldset>
      <fieldset id="agency-box">
        <legend>기관 선택</legend>
        <div id="agencies" class="agencies"></div>
        <p id="pause-note" class="warn" hidden>선택한 기관이 없습니다. 저장하면 모든 알림이 일시중지됩니다.</p>
      </fieldset>
      <p id="current" class="current"></p>
      <div class="row">
        <button type="submit" class="primary">설정 저장</button>
        <button type="button" id="logout" class="ghost">로그아웃</button>
      </div>
      <p class="hint">저장한 설정은 다음 모니터링 발송부터 적용됩니다. 로그인은 30분 동안 유지됩니다.</p>
    </form>
  </section>
</main>
</body>
</html>
`;

export const APP_CSS = `:root {
  --bg: #f1f5f9; --card: #ffffff; --text: #0f172a; --muted: #475569; --line: #e2e8f0;
  --accent: #1d4ed8; --accent-text: #ffffff; --warn-bg: #fffbeb; --warn: #92400e;
  --ok: #166534; --err: #b91c1c; --focus: #2563eb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b1220; --card: #111a2e; --text: #e2e8f0; --muted: #94a3b8; --line: #1e293b;
    --accent: #3b82f6; --accent-text: #ffffff; --warn-bg: #2a2110; --warn: #fbbf24;
    --ok: #4ade80; --err: #f87171; --focus: #60a5fa;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", Roboto, sans-serif;
  display: flex; justify-content: center; padding: 24px 16px;
}
.card {
  width: 100%; max-width: 560px; background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 24px; align-self: flex-start;
}
.brand { margin: 0; font-size: 11px; letter-spacing: 1.6px; font-weight: 600; color: var(--muted); }
h1 { margin: 6px 0 8px; font-size: 22px; }
.lead, .hint { color: var(--muted); font-size: 14px; line-height: 1.6; }
.hint { font-size: 13px; margin: 6px 0 14px; }
label { display: block; font-weight: 600; font-size: 14px; margin: 14px 0 6px; }
input[type="email"], input[type="text"] {
  width: 100%; padding: 12px; font-size: 16px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--card); color: var(--text);
}
input[aria-invalid="true"] { border-color: var(--err); }
fieldset { border: 1px solid var(--line); border-radius: 8px; margin: 14px 0; padding: 8px 14px 12px; }
legend { font-weight: 700; font-size: 14px; padding: 0 6px; }
.choice { display: flex; align-items: center; gap: 10px; font-weight: 500; margin: 10px 0; cursor: pointer; }
.choice input { width: 18px; height: 18px; }
.agencies .choice { font-weight: 400; }
fieldset:disabled { opacity: 0.55; }
.row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
button {
  font: inherit; font-size: 15px; padding: 11px 16px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text);
}
button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); font-weight: 600; }
button:disabled { opacity: 0.6; cursor: progress; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.status { min-height: 1.4em; font-size: 14px; margin: 12px 0 0; }
.status.ok { color: var(--ok); }
.status.err { color: var(--err); }
.status.busy { color: var(--muted); }
.warn { background: var(--warn-bg); color: var(--warn); padding: 10px 12px; border-radius: 6px; font-size: 13px; }
.current { font-size: 14px; color: var(--muted); }
@media (max-width: 420px) { .card { padding: 18px; } .row button { flex: 1 1 100%; } }
`;

export const APP_JS = `"use strict";
(function () {
  var state = { email: "", view: null, busy: false };
  var $ = function (id) { return document.getElementById(id); };
  var steps = ["step-email", "step-code", "step-prefs"];

  function show(step) {
    steps.forEach(function (id) { $(id).hidden = id !== step; });
    var first = $(step).querySelector("input:not([type=hidden]), button");
    if (first) first.focus();
  }

  function status(text, kind) {
    var el = $("status");
    el.textContent = text || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll("button").forEach(function (b) { b.disabled = busy; });
    if (busy) status("처리 중입니다…", "busy");
  }

  var MESSAGES = {
    invalid_email: "이메일 주소 형식을 확인해 주세요.",
    invalid_input: "인증번호 6자리를 정확히 입력해 주세요.",
    invalid_code: "인증번호가 올바르지 않거나 만료되었습니다. 번호를 다시 받아 주세요.",
    rate_limited: "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
    unauthorized: "로그인이 만료되었습니다. 다시 인증해 주세요.",
    version_conflict: "다른 화면에서 설정이 먼저 바뀌었습니다. 최신 설정을 불러왔으니 다시 확인해 주세요.",
    unknown_agency: "선택할 수 없는 기관이 포함되어 있습니다. 화면을 새로 고쳐 주세요.",
    network: "서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
  };
  function message(code) { return MESSAGES[code] || "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."; }

  function api(method, path, body) {
    var init = { method: method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { status: res.status, data: data };
      });
    }, function () { return { status: 0, data: { error: "network" } }; });
  }

  function validEmail(v) { return /^[^\\s@]+@[^\\s@]+$/.test(v); }

  function renderPrefs(view) {
    state.view = view;
    var box = $("agencies");
    while (box.firstChild) box.removeChild(box.firstChild);
    view.catalog.forEach(function (agency) {
      var label = document.createElement("label");
      label.className = "choice";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.name = "agency";
      input.value = agency.id;
      input.checked = view.mode === "selected" && view.agencies.indexOf(agency.id) !== -1;
      input.addEventListener("change", updateForm);
      var span = document.createElement("span");
      span.textContent = agency.name;
      label.appendChild(input);
      label.appendChild(span);
      box.appendChild(label);
    });
    document.querySelectorAll("input[name=mode]").forEach(function (r) { r.checked = r.value === view.mode; });
    $("current").textContent = "현재 설정: " + describe(view);
    updateForm();
  }

  function describe(view) {
    if (view.mode === "all") return "모든 기관 수신";
    if (view.agencies.length === 0) return "모든 알림 일시중지";
    var names = view.catalog.filter(function (c) { return view.agencies.indexOf(c.id) !== -1; })
      .map(function (c) { return c.name; });
    return "선택 수신 (" + names.join(", ") + ")";
  }

  function selectedMode() {
    var r = document.querySelector("input[name=mode]:checked");
    return r ? r.value : "all";
  }
  function selectedAgencies() {
    return Array.prototype.map.call(document.querySelectorAll("input[name=agency]:checked"), function (c) { return c.value; });
  }
  function updateForm() {
    var selected = selectedMode() === "selected";
    $("agency-box").disabled = !selected;
    $("pause-note").hidden = !(selected && selectedAgencies().length === 0);
  }

  function loadPrefs(afterSave) {
    return api("GET", "/api/preferences").then(function (r) {
      if (r.status === 200) {
        renderPrefs(r.data);
        if (!afterSave) { show("step-prefs"); status(""); }
        return r.data;
      }
      show("step-email");
      if (afterSave || r.status !== 401) status(message(r.data.error), "err");
      return null;
    });
  }

  function requestCode() {
    setBusy(true);
    return api("POST", "/api/otp/request", { email: state.email }).then(function (r) {
      setBusy(false);
      if (r.status === 202) {
        show("step-code");
        status("등록된 구독자라면 인증번호가 발송되었습니다. 메일함(스팸함 포함)을 확인해 주세요.", "ok");
      } else {
        status(message(r.data.error), "err");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("form-email").addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (state.busy) return;
      var v = $("email").value.trim();
      if (!validEmail(v)) {
        $("email").setAttribute("aria-invalid", "true");
        status(message("invalid_email"), "err");
        $("email").focus();
        return;
      }
      $("email").removeAttribute("aria-invalid");
      state.email = v;
      requestCode();
    });

    $("form-code").addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (state.busy) return;
      var code = $("code").value.trim();
      if (!/^[0-9]{6}$/.test(code)) {
        $("code").setAttribute("aria-invalid", "true");
        status(message("invalid_input"), "err");
        $("code").focus();
        return;
      }
      $("code").removeAttribute("aria-invalid");
      setBusy(true);
      api("POST", "/api/otp/verify", { email: state.email, code: code }).then(function (r) {
        setBusy(false);
        $("code").value = "";
        if (r.status === 200) return loadPrefs(false);
        status(message(r.data.error), "err");
      });
    });

    $("resend").addEventListener("click", function () { if (!state.busy) requestCode(); });
    $("change-email").addEventListener("click", function () { show("step-email"); status(""); });
    document.querySelectorAll("input[name=mode]").forEach(function (r) { r.addEventListener("change", updateForm); });

    $("form-prefs").addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (state.busy || !state.view) return;
      var mode = selectedMode();
      var body = { mode: mode, agencies: mode === "selected" ? selectedAgencies() : [], version: state.view.version };
      setBusy(true);
      api("PUT", "/api/preferences", body).then(function (r) {
        if (r.status === 200) {
          // 저장 응답만 믿지 않고 서버에서 다시 읽어 실제로 저장됐는지 확인한다.
          return loadPrefs(true).then(function (fresh) {
            setBusy(false);
            if (!fresh) return;
            var same = fresh.mode === body.mode &&
              JSON.stringify(fresh.agencies.slice().sort()) === JSON.stringify(body.agencies.slice().sort());
            status(same ? "저장되었습니다. 다음 발송부터 적용됩니다." : "저장 결과를 확인하지 못했습니다. 화면의 현재 설정을 확인해 주세요.", same ? "ok" : "err");
          });
        }
        setBusy(false);
        if (r.status === 409 && r.data.current) {
          renderPrefs(r.data.current);
        } else if (r.status === 401) {
          show("step-email");
        }
        status(message(r.data.error), "err");
      });
    });

    $("logout").addEventListener("click", function () {
      if (state.busy) return;
      setBusy(true);
      api("POST", "/api/logout", {}).then(function () {
        setBusy(false);
        state.view = null;
        show("step-email");
        status("로그아웃되었습니다.", "ok");
      });
    });

    loadPrefs(false);
  });
})();
`;
