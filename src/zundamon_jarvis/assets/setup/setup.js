// The setup page: one step at a time. Everything is saved by the setup process as soon as it is
// chosen; this page only shows /api/state (polled) and posts what was picked. No framework, no
// build step, no external requests. Text from the server is always set as text, never as HTML.
// The page's own words come from /assets/strings.json in state.lang (the header's switch posts
// /api/language; the server then answers in that language and the page redraws its step).
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  // ---- words ----

  let STR = null; // the catalog: {key: {ja, en}}
  let L = document.documentElement.lang || "ja";

  function t(key, fields) {
    const entry = STR && STR[key];
    let s = entry ? entry[L] || entry.ja : key;
    if (fields) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in fields ? String(fields[k]) : m));
    return s;
  }

  // The words with nodes in their {fields}: tn("login.howto", { cmd: code("claude") }).
  function tn(key, nodes) {
    return t(key).split(/\{(\w+)\}/).map((part, i) => (i % 2 ? nodes[part] : part));
  }

  async function loadStrings() {
    let r;
    try {
      r = await fetch("/assets/strings.json", { credentials: "same-origin", cache: "no-store" });
    } catch (e) {
      return false; // tried again on the next poll
    }
    if (r.status === 403) throw new ApiError("", 403);
    if (r.ok) {
      try {
        STR = await r.json();
      } catch (e) {
        /* tried again on the next poll */
      }
    }
    return STR !== null;
  }

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (k in node && typeof v !== "string") node[k] = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function code(text) {
    return el("code", { text });
  }

  function gb(bytes) {
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + " GB";
    if (bytes >= 1e6) return (bytes / 1e6).toFixed(0) + " MB";
    return Math.max(0, Math.round(bytes / 1e3)) + " KB";
  }

  function setBar(bar, fraction) {
    const fill = bar.firstChild;
    if (fraction === null) {
      bar.classList.add("indeterminate");
      fill.style.width = "";
    } else {
      bar.classList.remove("indeterminate");
      fill.style.width = Math.round(Math.min(1, Math.max(0, fraction)) * 100) + "%";
    }
  }

  function bar() {
    return el("div", { class: "bar", role: "presentation" }, el("i"));
  }

  function banner(kind, ...children) {
    return el("div", { class: "banner banner-" + kind }, ...children);
  }

  // ---- talking to the setup process ----

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  let S = null; // the latest /api/state
  let offline = false;

  async function api(path, body) {
    const post = body !== undefined;
    let r;
    try {
      r = await fetch(path, {
        method: post ? "POST" : "GET",
        headers: post ? { "Content-Type": "application/json" } : {},
        body: post ? JSON.stringify(body) : undefined,
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch (e) {
      setOffline(true);
      throw new ApiError(t("api.offline"), 0);
    }
    setOffline(false);
    let data = {};
    try {
      data = await r.json();
    } catch (e) {
      data = {};
    }
    if (!r.ok) throw new ApiError(data.error || r.statusText, r.status);
    const state = post ? data.state : data;
    if (state) applyState(state);
    return data;
  }

  function send(path, body) {
    // Fire and forget: the result shows up in the state; a refusal is shown on the step.
    return api(path, body || {}).catch((e) => showError(e));
  }

  function setOffline(v) {
    if (offline === v) return;
    offline = v;
    $("offline").hidden = !v;
  }

  let errorBox = null;
  function showError(e) {
    if (!(e instanceof ApiError) || e.status === 0) return;
    if (errorBox) errorBox.remove();
    errorBox = banner("err", e.message);
    $("step-body").prepend(errorBox);
  }

  // ---- the steps ----

  const sys = () => (S ? S.system : "");
  const prereqDone = () => S && S.prereq.engine.state === "done" && S.prereq.model.state === "done";
  const loginOk = () => S && (S.login.state === "ok" || S.login.state === "limit");

  function downloadText(item, isEngine) {
    switch (item.state) {
      case "checking":
        return t("dl.checking");
      case "waiting":
        return t("dl.waiting");
      case "done":
        return t("dl.done");
      case "error":
        return t("dl.error", { error: item.error || "" });
      case "running":
        if (isEngine && item.stage === "extract") return t("dl.extract");
        if (isEngine && item.stage === "check") return t("dl.check");
        if (item.total) {
          return t("dl.progress", { done: gb(item.done), total: gb(item.total),
            pct: Math.floor((item.done / item.total) * 100) });
        }
        return item.done ? t("dl.got", { done: gb(item.done) }) : t("dl.starting");
      default:
        return "";
    }
  }

  function downloadFraction(item, isEngine) {
    if (item.state === "done") return 1;
    if (item.state !== "running") return 0;
    if (isEngine && item.stage && item.stage !== "download") return null;
    return item.total ? item.done / item.total : null;
  }

  const ICON = { done: "✅", error: "⚠️", running: "⏬", checking: "…", waiting: "⏳" };

  const prereqStep = {
    id: "prereq",
    label: "prereq.label",
    title: "prereq.title",
    bubble: "prereq.bubble",
    enter(body) {
      this.rows = {};
      const items = [
        ["engine", t("prereq.engine"), t("prereq.engine_about")],
        ["model", t("prereq.model"), t("prereq.model_about")],
      ];
      body.append(el("p", { text: t("prereq.intro") }));
      const list = el("div", { class: "stack" });
      for (const [key, name, about] of items) {
        const icon = el("span", { class: "icon", "aria-hidden": "true" });
        const detail = el("span", { class: "detail" });
        const b = bar();
        list.append(el("div", { class: "item" }, icon, el("span", { class: "what", text: name }),
          el("span", { class: "detail", text: about }), detail, b));
        this.rows[key] = { icon, detail, bar: b };
      }
      this.retry = el("button", { class: "btn", type: "button", text: t("prereq.retry"),
        onclick: () => send("/api/prereq/retry") });
      this.retryRow = el("div", { class: "row" }, this.retry);
      body.append(list, this.retryRow);
    },
    update() {
      for (const key of ["engine", "model"]) {
        const item = S.prereq[key];
        const row = this.rows[key];
        row.icon.textContent = ICON[item.state] || "";
        row.detail.textContent = downloadText(item, key === "engine");
        row.detail.className = "detail" + (item.state === "done" ? " status-ok" : "");
        setBar(row.bar, downloadFraction(item, key === "engine"));
        row.bar.hidden = item.state === "done" || item.state === "error";
      }
      this.retryRow.hidden = !(S.prereq.engine.state === "error" || S.prereq.model.state === "error");
    },
    next: () => [true, prereqDone() ? "" : t("prereq.hint")],
  };

  const loginStep = {
    id: "login",
    label: "login.label",
    title: "login.title",
    bubble: "login.bubble",
    enter(body) {
      if (S.login.state === "idle") send("/api/login/check");
      this.noteKey = undefined;
      this.status = el("p", { class: "status" });
      this.bar = bar();
      this.note = el("div");
      this.again = el("button", { class: "btn", type: "button", text: t("login.again"),
        onclick: () => send("/api/login/check") });
      body.append(el("p", { class: "muted small", text: t("login.about") }),
        this.status, this.bar, this.note, el("div", { class: "row" }, this.again));
    },
    update() {
      const st = S.login.state;
      this.bar.hidden = st !== "running";
      setBar(this.bar, null);
      this.again.hidden = st === "running" || st === "ok";
      this.status.className = st === "ok" ? "status status-ok" : "status";
      this.status.textContent = {
        idle: "",
        running: t("login.running"),
        ok: t("login.ok"),
        limit: t("login.limit"),
        login: "",
      }[st] || "";
      const key = st + "|" + S.login.note;
      if (this.noteKey === key) return;
      this.noteKey = key;
      this.note.replaceChildren();
      if (st === "login") {
        this.note.append(banner("err", el("p", { text: S.login.note }),
          el("p", {}, ...tn("login.howto", { cmd: code("claude") }))));
      } else if (st === "limit") {
        this.note.append(banner("warn", el("p", { text: S.login.note })));
      }
    },
    next: () => [loginOk(), loginOk() ? "" : t("login.hint")],
  };

  const micStep = {
    id: "mic",
    label: "mic.label",
    title: "mic.title",
    bubble: "mic.bubble",
    fast: true,
    enter(body, again) {
      this.choicesKey = null;
      this.select = el("select", { id: "mic-select", onchange: () =>
        send("/api/mic/start", { index: Number(this.select.value) }) });
      this.meter = el("div", { class: "meter", role: "meter", "aria-label": t("mic.meter"),
        "aria-valuemin": "0", "aria-valuemax": "100" }, el("i"));
      this.status = el("p", { class: "status" });
      this.note = el("div");
      const reopen = el("button", { class: "btn btn-small", type: "button", text: t("mic.reopen"),
        onclick: () => send("/api/mic/start") });
      body.append(
        el("label", { class: "field", for: "mic-select" }, t("mic.field"), this.select),
        this.meter, this.status, this.note,
        el("div", { class: "row" }, reopen),
      );
      if (sys() === "mac") {
        body.append(el("p", { class: "muted small", text: t("mic.mac_hint") }));
      }
      if (!again) send("/api/mic/start"); // a redraw in another language keeps the meter running
    },
    leave() {
      send("/api/mic/stop");
    },
    update() {
      const m = S.mic;
      const key = JSON.stringify(m.choices);
      if (key !== this.choicesKey && document.activeElement !== this.select) {
        this.choicesKey = key;
        this.select.replaceChildren(...m.choices.map((c, i) =>
          el("option", { value: String(i), selected: c.selected, text: c.label })));
      }
      const db = 20 * Math.log10(Math.max(m.level, 1e-4));
      const pct = Math.round(Math.min(1, Math.max(0, (db + 60) / 60)) * 100);
      this.meter.firstChild.style.width = pct + "%";
      this.meter.setAttribute("aria-valuenow", String(pct));
      this.status.className = m.status === "ok" ? "status status-ok" : "status";
      this.status.textContent = {
        idle: "",
        opening: t("mic.opening"),
        listening: t("mic.listening"),
        ok: t("mic.ok", { peak: m.peak.toFixed(3) }),
        silent: t("mic.silent"),
        error: t("mic.error"),
      }[m.status] || "";
      const nkey = [m.note, m.fallback_note].join("|");
      if (nkey === this.noteKey) return;
      this.noteKey = nkey;
      this.note.replaceChildren();
      if (m.fallback_note) this.note.append(banner("warn", m.fallback_note));
      if (m.note) this.note.append(banner("warn", m.note));
    },
    next: () => [true, ""],
  };

  const headphonesStep = {
    id: "headphones",
    label: "hp.label",
    title: "hp.title",
    bubble: "hp.bubble",
    enter(body) {
      body.append(el("p", { text: S.headphones }));
      body.append(el("p", { class: "muted small", text: t("hp.later") }));
    },
    update() {},
    next: () => [true, ""],
  };

  const voiceStep = {
    id: "voice",
    label: "voice.label",
    title: "voice.title",
    bubble: "voice.bubble",
    enter(body, again) {
      if (!again) send("/api/voice/open");
      this.cardsKey = null;
      this.status = el("div");
      this.bar = bar();
      this.current = el("p", { class: "muted" });
      this.cards = el("div", { class: "choices", role: "list" });
      this.error = el("div");
      const v = S.voice;
      this.speed = el("input", { type: "range", id: "speed", min: String(v.speed_min), max: String(v.speed_max),
        step: "0.05", value: String(v.speed) });
      this.speedOut = el("output", { for: "speed", text: t("speed.value", { speed: v.speed.toFixed(2) }) });
      this.speed.addEventListener("input", () => {
        this.speedOut.textContent = t("speed.value", { speed: Number(this.speed.value).toFixed(2) });
      });
      this.speed.addEventListener("change", () => send("/api/speed", { speed: Number(this.speed.value) }));
      body.append(this.status, this.bar, this.current, this.cards, this.error,
        el("label", { class: "field", for: "speed" }, t("speed.field")),
        el("div", { class: "slider" }, el("span", { class: "small muted", text: t("speed.slow") }), this.speed,
          el("span", { class: "small muted", text: t("speed.fast") }), this.speedOut),
        el("p", { class: "muted small", text: t("voice.speed_note") }));
    },
    update() {
      const v = S.voice;
      this.bar.hidden = !(v.state === "waiting" || v.state === "starting");
      setBar(this.bar, v.state === "waiting" ? downloadFraction(S.prereq.engine, true) : null);
      const statusKey = v.state + "|" + v.message;
      if (statusKey !== this.statusKey) {
        this.statusKey = statusKey;
        this.status.replaceChildren();
        if (v.state === "waiting") {
          this.status.append(el("p", { text: t("voice.waiting") }));
        } else if (v.state === "starting") {
          this.status.append(el("p", { text: v.message || t("voice.connecting") }));
        } else if (v.state === "down") {
          this.status.append(banner("warn", el("p", { text: v.message }),
            el("button", { class: "btn btn-small", type: "button", text: t("voice.reconnect"),
              onclick: () => send("/api/voice/open") })));
        }
      }
      this.current.textContent = v.current_label ? t("voice.current", { voice: v.current_label }) : "";
      const key = JSON.stringify([v.choices, v.current, v.previewing, v.state]);
      if (key !== this.cardsKey) {
        this.cardsKey = key;
        const focused = document.activeElement && document.activeElement.dataset.key;
        this.cards.replaceChildren(...v.choices.map((c) => {
          const selected = c.speaker_id === v.current;
          const playing = v.previewing === c.speaker_id;
          const card = el("div", { class: "choice" + (selected ? " selected" : ""), role: "listitem" },
            el("span", { class: "name", text: c.label }),
            el("span", { class: "tag", text: selected ? t("voice.tag") : "" }),
            el("div", { class: "actions" },
              el("button", { class: "btn btn-small", type: "button", "data-key": "p" + c.speaker_id,
                disabled: v.previewing !== null || v.state !== "ready",
                text: playing ? t("voice.playing") : t("voice.preview"),
                onclick: () => send("/api/voice/preview", { speaker_id: c.speaker_id }) }),
              el("button", { class: "btn btn-small" + (selected ? "" : " btn-primary"), type: "button",
                "data-key": "s" + c.speaker_id, disabled: selected,
                text: selected ? t("voice.chosen") : t("voice.choose"),
                onclick: () => send("/api/voice/select", { speaker_id: c.speaker_id }) })));
          return card;
        }));
        if (focused) {
          const again = this.cards.querySelector(`[data-key="${focused}"]`);
          if (again && !again.disabled) again.focus();
        }
      }
      const ekey = v.error || "";
      if (ekey !== this.errorKey) {
        this.errorKey = ekey;
        this.error.replaceChildren(...(v.error ? [banner("warn", v.error)] : []));
      }
    },
    next: () => [true, ""],
  };

  const knowledgeStep = {
    id: "knowledge",
    label: "k.label",
    title: "k.title",
    bubble: "k.bubble",
    enter(body, again) {
      const k = S.knowledge;
      const typed = again && this.input ? this.input.value : null; // not saved yet: kept on a redraw
      this.input = el("input", { type: "text", id: "kdir", value: typed ?? (k.value || ""), placeholder: t("k.placeholder"),
        autocomplete: "off", spellcheck: "false" });
      this.input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") this.save();
      });
      this.msg = el("div");
      const pick = k.can_pick ? el("button", { class: "btn", type: "button", text: t("k.pick"),
        onclick: () => this.pick() }) : null;
      this.clear = el("button", { class: "btn btn-ghost", type: "button", text: t("k.clear"),
        onclick: () => this.save("") });
      body.append(
        el("p", { text: t("k.about") }),
        el("label", { class: "field", for: "kdir" }, t("k.field")),
        el("div", { class: "row" }, this.input,
          el("button", { class: "btn", type: "button", text: t("k.save"), onclick: () => this.save() }), pick, this.clear),
        el("p", { class: "muted small", text: t("k.hint") }),
        this.msg,
      );
    },
    async save(value) {
      const path = value === undefined ? this.input.value.trim() : value;
      try {
        await api("/api/knowledge", { path });
        this.input.value = S.knowledge.value || "";
        this.msg.replaceChildren(banner("ok", S.knowledge.value ? t("k.set", { path: S.knowledge.value })
          : t("k.none")));
        return true;
      } catch (e) {
        this.msg.replaceChildren(banner("err", e.message));
        return false;
      }
    },
    async pick() {
      this.msg.replaceChildren(el("p", { class: "muted", text: t("k.picking") }));
      try {
        const r = await api("/api/knowledge/pick", {});
        if (r.path) {
          this.input.value = r.path;
          await this.save();
        } else {
          this.msg.replaceChildren();
        }
      } catch (e) {
        this.msg.replaceChildren(banner("err", e.message));
      }
    },
    update() {
      this.clear.hidden = !S.knowledge.value;
    },
    async beforeNext() {
      const typed = this.input.value.trim();
      if (!typed || typed === (S.knowledge.value || "")) return true;
      return this.save();
    },
    next: () => [true, ""],
  };

  const modelStep = {
    id: "model",
    label: "model.label",
    title: "model.title",
    bubble: "model.bubble",
    enter(body) {
      this.radios = {};
      const group = el("div", { class: "choices", role: "radiogroup", "aria-label": t("model.group") });
      for (const m of S.model.choices) {
        const input = el("input", { type: "radio", name: "model", value: m,
          onchange: () => send("/api/model", { model: m }) });
        this.radios[m] = { input, card: null };
        const card = el("label", { class: "choice" }, input, el("span", { class: "name", text: m }),
          el("span", { class: "small muted", text: STR && STR["model." + m] ? t("model." + m) : "" }));
        this.radios[m].card = card;
        group.append(card);
      }
      this.current = el("p", { class: "muted small" });
      body.append(group, this.current,
        el("p", { class: "muted small", text: t("model.note") }));
    },
    update() {
      for (const [m, r] of Object.entries(this.radios)) {
        r.input.checked = S.model.current === m;
        r.card.classList.toggle("selected", S.model.current === m);
      }
      this.current.textContent = S.model.current ? "" : t("model.default_now");
    },
    next: () => [true, ""],
  };

  const STAGE_TEXT = { install: "start.install", wait: "start.wait" };

  const startStep = {
    id: "start",
    label: "start.label",
    title: "start.title",
    bubble: "start.bubble",
    enter(body) {
      const micLabel = (S.mic.choices.find((c) => c.selected) || {}).label || t("mic.default");
      body.append(el("dl", { class: "summary" },
        el("dt", { text: t("sum.voice") }), el("dd", { text: S.voice.current_label || "" }),
        el("dt", { text: t("sum.speed") }), el("dd", { text: t("speed.value", { speed: S.voice.speed.toFixed(2) }) }),
        el("dt", { text: t("sum.mic") }), el("dd", { text: micLabel }),
        el("dt", { text: t("sum.knowledge") }), el("dd", { text: S.knowledge.value || t("sum.none") }),
        el("dt", { text: t("sum.model") }), el("dd", { text: S.model.current || t("sum.model_default") }),
        el("dt", { text: t("sum.config") }), el("dd", {}, code(S.config_path))));
      this.ready = el("div");
      this.go = el("button", { class: "btn btn-primary", type: "button", text: t("start.go"),
        onclick: () => send("/api/start") });
      this.goRow = el("div", { class: "row" }, this.go);
      this.progress = el("p");
      this.bar = bar();
      this.result = el("div");
      body.append(this.ready, this.goRow, this.progress, this.bar, this.result);
    },
    update() {
      const st = S.start;
      const waitingFor = [];
      if (!prereqDone()) waitingFor.push("downloads");
      if (!loginOk()) waitingFor.push("login");
      const rkey = waitingFor.join() + "|" + JSON.stringify(S.prereq);
      if (rkey !== this.readyKey) {
        this.readyKey = rkey;
        this.ready.replaceChildren();
        if (!prereqDone()) {
          const e = S.prereq.engine;
          const m = S.prereq.model;
          this.ready.append(banner("warn",
            el("p", { text: t("start.after_downloads") }),
            el("p", { class: "small", text: t("start.engine", { status: downloadText(e, true) }) }),
            el("p", { class: "small", text: t("start.model", { status: downloadText(m, false) }) })));
        } else if (!loginOk()) {
          this.ready.append(banner("err", t("start.login_first")));
        }
      }
      this.go.disabled = waitingFor.length > 0 || st.state === "running";
      this.go.textContent = st.state === "error" ? t("start.retry") : t("start.go");
      this.goRow.hidden = st.state === "done" || st.state === "running";
      this.bar.hidden = st.state !== "running";
      setBar(this.bar, null);
      this.progress.textContent = st.state === "running"
        ? (st.stage === "daemon_mic" ? t("start.daemon_mic") : STAGE_TEXT[st.stage] ? t(STAGE_TEXT[st.stage]) : "")
        : "";
      const key = JSON.stringify([st.state, st.message, st.status_text, st.daemon_mic, st.daemon_mic_text]);
      if (key === this.resultKey) return;
      this.resultKey = key;
      this.result.replaceChildren();
      if (st.state === "error") {
        this.result.append(banner("err", st.message));
        return;
      }
      if (st.status_text) {
        this.result.append(el("p", { class: "status-ok", text: t("start.started", { status: st.status_text }) }));
      } else if (st.state === "done" && st.message) {
        this.result.append(banner("warn", st.message));
      }
      if (st.daemon_mic) {
        const kind = { checking: "ok", ok: "ok", silent: "warn", unknown: "warn" }[st.daemon_mic];
        const box = banner(kind, el("p", { class: "pre-line", text: st.daemon_mic_text }));
        if (st.daemon_mic === "silent" || st.daemon_mic === "unknown") {
          box.append(el("button", { class: "btn btn-small", type: "button", text: t("start.recheck"),
            onclick: () => send("/api/daemon-mic/check") }));
        }
        this.result.append(box);
      }
    },
    next: () => [S.start.state === "done" && S.start.daemon_mic !== "checking",
      S.start.state === "done" ? "" : t("start.hint")],
  };

  function usage() {
    const s = sys();
    const parts = [];
    parts.push(el("p", { text: t(s === "mac" ? "use.mac" : s === "windows" ? "use.windows" : "use.linux") }));
    parts.push(el("ul", { class: "tips" },
      el("li", {}, code("zundamon-jarvisctl toggle"), t("use.toggle")),
      el("li", {}, code("zundamon-jarvisctl watch"), t("use.watch")),
      el("li", {}, code("zundamon-jarvisctl settings"), t("use.settings")),
      el("li", {}, code("zundamon-jarvis setup"), t("use.setup")),
      el("li", {}, code("zundamon-jarvis doctor"), t("use.doctor"))));
    return parts;
  }

  const doneStep = {
    id: "done",
    label: "done.label",
    title: "done.title",
    bubble: "done.bubble",
    hidden: true,
    enter(body) {
      body.append(el("p", { text: t("done.try") }), ...usage());
      this.closeBtn = el("button", { class: "btn btn-primary", type: "button", text: t("done.close"),
        onclick: () => this.finish() });
      body.append(el("div", { class: "row" }, this.closeBtn));
    },
    async finish() {
      this.closeBtn.disabled = true;
      try {
        await api("/api/finish", {});
      } catch (e) {
        showError(e);
        this.closeBtn.disabled = false;
        return;
      }
      finished = true;
      clearTimeout(timer);
      this.closed();
    },
    closed() {
      $("step-body").replaceChildren(el("p", { text: t("done.closed") }));
      $("back").hidden = true;
      $("next").hidden = true;
      $("nav-hint").textContent = "";
    },
    update() {},
    next: () => [false, ""],
  };

  const STEPS = [prereqStep, loginStep, micStep, headphonesStep, voiceStep, knowledgeStep, modelStep,
    startStep, doneStep];

  // ---- navigation ----

  let cur = 0;
  let reached = 0;
  let finished = false;

  function stepDone(i) {
    const id = STEPS[i].id;
    if (id === "prereq") return prereqDone();
    if (id === "login") return loginOk();
    if (id === "start") return S.start.state === "done";
    return i < reached || S.start.state === "done";
  }

  function renderRail() {
    const ol = $("steps");
    if (!ol.childElementCount || ol.dataset.lang !== L) {
      ol.replaceChildren();
      ol.dataset.lang = L;
      STEPS.forEach((step, i) => {
        if (step.hidden) return;
        ol.append(el("li", { "data-i": String(i) },
          el("button", { type: "button", onclick: () => go(i) },
            el("span", { class: "num", text: String(i + 1) }), el("span", { class: "label", text: t(step.label) }))));
      });
    }
    for (const li of ol.children) {
      const i = Number(li.dataset.i);
      const done = stepDone(i);
      li.classList.toggle("current", i === cur);
      li.classList.toggle("done", done && i !== cur);
      li.firstChild.firstChild.textContent = done && i !== cur ? "✓" : String(i + 1);
      li.firstChild.disabled = i > reached;
      if (i === cur) li.firstChild.setAttribute("aria-current", "step");
      else li.firstChild.removeAttribute("aria-current");
    }
    const mini = $("dl-mini");
    const busy = S.prereq.engine.state !== "done" || S.prereq.model.state !== "done";
    mini.hidden = !busy || STEPS[cur].id === "prereq";
    if (!mini.hidden) {
      const e = S.prereq.engine;
      const m = S.prereq.model;
      const item = e.state !== "done" ? e : m;
      const key = e.state !== "done" ? "mini.engine" : "mini.model";
      if (!mini.firstChild) mini.append(el("div"), bar());
      mini.firstChild.textContent = t(key, { status: downloadText(item, item === e) });
      setBar(mini.lastChild, downloadFraction(item, item === e));
    }
  }

  function updateNav() {
    const step = STEPS[cur];
    const [ok, hint] = step.next();
    $("back").disabled = cur === 0;
    $("back").hidden = finished;
    $("next").hidden = step.id === "done" || finished;
    $("next").disabled = !ok;
    $("next").textContent = step.id === "start" ? t("nav.finish") : t("nav.next");
    $("nav-hint").textContent = hint;
  }

  // again: the same step drawn again in another language (no new requests, typed text kept)
  function render(again) {
    const step = STEPS[cur];
    errorBox = null;
    $("step-title").textContent = t(step.title);
    $("bubble").textContent = t(step.bubble);
    const body = $("step-body");
    body.replaceChildren();
    for (const s of STEPS) {
      s.statusKey = s.noteKey = s.resultKey = s.readyKey = s.errorKey = s.choicesKey = undefined;
    }
    step.enter(body, again === true);
    step.update();
    renderRail();
    updateNav();
    document.title = t("page.step_title", { step: t(step.label) });
  }

  // The page's fixed words (index.html: data-t="key" for the text, data-t-aria-label="key").
  function relabel() {
    document.documentElement.lang = L;
    for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
    for (const node of document.querySelectorAll("[data-t-aria-label]")) {
      node.setAttribute("aria-label", t(node.dataset.tAriaLabel));
    }
    $("offline").replaceChildren(...tn("offline", { cmd: code("zundamon-jarvis setup") }));
    $("back").textContent = t("nav.back");
    for (const b of document.querySelectorAll("#lang button")) {
      b.setAttribute("aria-pressed", String(b.dataset.lang === L));
    }
    $("lang").hidden = false;
  }

  async function go(i) {
    if (i === cur || i < 0 || i >= STEPS.length || i > reached + 1) return;
    const step = STEPS[cur];
    if (i > cur && step.beforeNext && !(await step.beforeNext())) return;
    if (step.leave) step.leave();
    cur = i;
    reached = Math.max(reached, cur);
    try {
      sessionStorage.setItem("zj-setup-step", JSON.stringify([cur, reached]));
    } catch (e) {
      /* storage may be off: the step is just not remembered across reloads */
    }
    render();
    $("step-title").focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }

  function applyState(state) {
    const first = S === null;
    const relang = !first && (state.lang || "ja") !== L;
    S = state;
    L = state.lang || "ja";
    if (first) {
      relabel();
      try {
        const saved = JSON.parse(sessionStorage.getItem("zj-setup-step") || "null");
        if (Array.isArray(saved)) [cur, reached] = saved.map((n) => Math.min(Number(n) || 0, STEPS.length - 1));
      } catch (e) {
        /* nothing remembered */
      }
      if (cur > 1 && !loginOk()) cur = 1; // a reload of a new run: the login must be checked again
      render();
      return;
    }
    if (relang) {
      relabel();
      if (finished) doneStep.closed();
      else render(true);
      return;
    }
    if (finished) return;
    STEPS[cur].update();
    renderRail();
    updateNav();
  }

  // ---- polling ----

  let timer = 0;

  async function poll() {
    try {
      if (STR === null && !(await loadStrings())) throw new ApiError("", 0);
      await api("/api/state");
    } catch (e) {
      if (e.status === 403) {
        setOffline(false);
        $("step-body").replaceChildren(banner("err", t("expired")));
        return;
      }
    }
    if (!finished) timer = setTimeout(poll, STEPS[cur].fast ? 120 : 700);
  }

  for (const b of document.querySelectorAll("#lang button")) {
    b.addEventListener("click", () => {
      if (b.dataset.lang !== L) send("/api/language", { language: b.dataset.lang });
    });
  }
  $("back").addEventListener("click", () => go(cur - 1));
  $("next").addEventListener("click", () => go(cur + 1));
  $("step-title").tabIndex = -1;

  window.addEventListener("pagehide", () => {
    if (finished) return;
    try {
      fetch("/api/bye", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
        body: "{}", credentials: "same-origin" });
    } catch (e) {
      /* the page is going away anyway */
    }
  });

  poll();
})();
