// The setup page: one step at a time. Everything is saved by the setup process as soon as it is
// chosen; this page only shows /api/state (polled) and posts what was picked. No framework, no
// build step, no external requests. Text from the server is always set as text, never as HTML.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

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
      throw new ApiError("つながりません", 0);
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
        return "確かめています…";
      case "waiting":
        return "順番待ち（声のエンジンのあとに取得します）";
      case "done":
        return "準備できています";
      case "error":
        return "取得できませんでした: " + (item.error || "");
      case "running":
        if (isEngine && item.stage === "extract") return "展開しています…";
        if (isEngine && item.stage === "check") return "動くか確かめています…";
        if (item.total) return `${gb(item.done)} / ${gb(item.total)}（${Math.floor((item.done / item.total) * 100)}%）`;
        return item.done ? `${gb(item.done)} 取得しました` : "取得を始めています…";
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
    label: "準備",
    title: "声のエンジンと文字起こしのモデル",
    bubble: "まずはボクの準備をするのだ！",
    enter(body) {
      this.rows = {};
      const items = [
        ["engine", "声のエンジン（VOICEVOX）", "ずんだもんの声を作るソフトです。"],
        ["model", "文字起こしのモデル（Whisper）", "話した声を文字にします。音声はこのパソコンの外に出ません。"],
      ];
      body.append(
        el("p", { text: "はじめての設定では、あわせて数 GB をダウンロードします（10〜20 分ほど）。" +
          "終わるのを待たずに次へ進めます。ダウンロードは裏で続きます。" }),
      );
      const list = el("div", { class: "stack" });
      for (const [key, name, about] of items) {
        const icon = el("span", { class: "icon", "aria-hidden": "true" });
        const detail = el("span", { class: "detail" });
        const b = bar();
        list.append(el("div", { class: "item" }, icon, el("span", { class: "what", text: name }),
          el("span", { class: "detail", text: about }), detail, b));
        this.rows[key] = { icon, detail, bar: b };
      }
      this.retry = el("button", { class: "btn", type: "button", text: "もう一度ダウンロードする",
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
    next: () => [true, prereqDone() ? "" : "ダウンロード中でも先へ進めます"],
  };

  const loginStep = {
    id: "login",
    label: "Claude ログイン",
    title: "Claude にログインできているか確かめます",
    bubble: "Claude とお話しできるか見てみるのだ",
    enter(body) {
      if (S.login.state === "idle") send("/api/login/check");
      this.status = el("p", { class: "status" });
      this.bar = bar();
      this.note = el("div");
      this.again = el("button", { class: "btn", type: "button", text: "もう一度確かめる",
        onclick: () => send("/api/login/check") });
      body.append(el("p", { class: "muted small", text: "Claude に短い質問をひとつ送って、返事が来るかを見ます。" }),
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
        running: "確かめています…（1 分ほどかかることがあります）",
        ok: "✅ ログインできています",
        limit: "ログインはできています（いまは利用上限です）",
        login: "ログインできていません",
      }[st] || "";
      const key = st + "|" + S.login.note;
      if (this.noteKey === key) return;
      this.noteKey = key;
      this.note.replaceChildren();
      if (st === "login") {
        this.note.append(banner("err", el("p", { text: S.login.note }),
          el("p", {}, "ターミナルで ", code("claude"), " と入力して Enter を押し、表示に従ってログインしてください。")));
      } else if (st === "limit") {
        this.note.append(banner("warn", el("p", { text: S.login.note })));
      }
    },
    next: () => [loginOk(), loginOk() ? "" : "ログインを確かめると次へ進めます"],
  };

  const micStep = {
    id: "mic",
    label: "マイク",
    title: "マイクを選びます",
    bubble: "マイクに向かって何か話してみるのだ！",
    fast: true,
    enter(body) {
      this.choicesKey = null;
      this.select = el("select", { id: "mic-select", onchange: () =>
        send("/api/mic/start", { index: Number(this.select.value) }) });
      this.meter = el("div", { class: "meter", role: "meter", "aria-label": "マイクの音の大きさ",
        "aria-valuemin": "0", "aria-valuemax": "100" }, el("i"));
      this.status = el("p", { class: "status" });
      this.note = el("div");
      const reopen = el("button", { class: "btn btn-small", type: "button", text: "マイクを開きなおす",
        onclick: () => send("/api/mic/start") });
      body.append(
        el("label", { class: "field", for: "mic-select" }, "使うマイク", this.select),
        this.meter, this.status, this.note,
        el("div", { class: "row" }, reopen),
      );
      if (sys() === "mac") {
        body.append(el("p", { class: "muted small", text: "はじめてのときは「“ターミナル”がマイクへのアクセスを求めています」" +
          "と出ます。「許可」を押してください（設定はターミナルから動いているためです）。" }));
      }
      send("/api/mic/start");
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
        opening: "マイクを開いています…",
        listening: "聞いています。何か話してみてください…",
        ok: `✅ 音が届いています（いちばん大きい音: ${m.peak.toFixed(3)}）`,
        silent: "音が取れていません（ずっと無音です）",
        error: "マイクを開けませんでした",
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
    label: "ヘッドホン",
    title: "ヘッドホンがおすすめです",
    bubble: "ヘッドホンだと、ボクがしゃべっている途中でも止められるのだ",
    enter(body) {
      body.append(el("p", { text: S.headphones }));
      body.append(el("p", { class: "muted small", text: "あとから変えても大丈夫です。" +
        "スピーカーのままでも使えます。" }));
    },
    update() {},
    next: () => [true, ""],
  };

  const voiceStep = {
    id: "voice",
    label: "声",
    title: "声を選びます",
    bubble: "好きな声を選ぶのだ。試聴はパソコンのスピーカーから鳴るのだ",
    enter(body) {
      send("/api/voice/open");
      this.cardsKey = null;
      this.status = el("div");
      this.bar = bar();
      this.current = el("p", { class: "muted" });
      this.cards = el("div", { class: "choices", role: "list" });
      this.error = el("div");
      const v = S.voice;
      this.speed = el("input", { type: "range", id: "speed", min: String(v.speed_min), max: String(v.speed_max),
        step: "0.05", value: String(v.speed) });
      this.speedOut = el("output", { for: "speed", text: v.speed.toFixed(2) + " 倍" });
      this.speed.addEventListener("input", () => {
        this.speedOut.textContent = Number(this.speed.value).toFixed(2) + " 倍";
      });
      this.speed.addEventListener("change", () => send("/api/speed", { speed: Number(this.speed.value) }));
      body.append(this.status, this.bar, this.current, this.cards, this.error,
        el("label", { class: "field", for: "speed" }, "話す速さ"),
        el("div", { class: "slider" }, el("span", { class: "small muted", text: "ゆっくり" }), this.speed,
          el("span", { class: "small muted", text: "はやく" }), this.speedOut),
        el("p", { class: "muted small", text: "試聴は、いま選んでいる速さで読み上げます。" }));
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
          this.status.append(el("p", { text: "声のエンジンのダウンロードが終わるのを待っています…" }));
        } else if (v.state === "starting") {
          this.status.append(el("p", { text: v.message || "VOICEVOX につないでいます…" }));
        } else if (v.state === "down") {
          this.status.append(banner("warn", el("p", { text: v.message }),
            el("button", { class: "btn btn-small", type: "button", text: "もう一度つなぐ",
              onclick: () => send("/api/voice/open") })));
        }
      }
      this.current.textContent = v.current_label ? "今の声: " + v.current_label : "";
      const key = JSON.stringify([v.choices, v.current, v.previewing, v.state]);
      if (key !== this.cardsKey) {
        this.cardsKey = key;
        const focused = document.activeElement && document.activeElement.dataset.key;
        this.cards.replaceChildren(...v.choices.map((c) => {
          const selected = c.speaker_id === v.current;
          const playing = v.previewing === c.speaker_id;
          const card = el("div", { class: "choice" + (selected ? " selected" : ""), role: "listitem" },
            el("span", { class: "name", text: c.label }),
            el("span", { class: "tag", text: selected ? "✓ 今の声" : "" }),
            el("div", { class: "actions" },
              el("button", { class: "btn btn-small", type: "button", "data-key": "p" + c.speaker_id,
                disabled: v.previewing !== null || v.state !== "ready",
                text: playing ? "再生中…" : "▶ 試聴",
                onclick: () => send("/api/voice/preview", { speaker_id: c.speaker_id }) }),
              el("button", { class: "btn btn-small" + (selected ? "" : " btn-primary"), type: "button",
                "data-key": "s" + c.speaker_id, disabled: selected,
                text: selected ? "選んでいます" : "この声にする",
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
    label: "知識フォルダ",
    title: "知識フォルダ（なくても使えます）",
    bubble: "メモやノートのフォルダがあれば、中身も見ながら答えるのだ",
    enter(body) {
      const k = S.knowledge;
      this.input = el("input", { type: "text", id: "kdir", value: k.value || "", placeholder: "例: ~/notes",
        autocomplete: "off", spellcheck: "false" });
      this.input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") this.save();
      });
      this.msg = el("div");
      const pick = k.can_pick ? el("button", { class: "btn", type: "button", text: "フォルダを選ぶ…",
        onclick: () => this.pick() }) : null;
      this.clear = el("button", { class: "btn btn-ghost", type: "button", text: "外す",
        onclick: () => this.save("") });
      body.append(
        el("p", { text: "メモやノートのフォルダを指定すると、Claude がその中身を読んだり書いたりしながら答えます。" +
          "フォルダの CLAUDE.md も読み込まれます。" }),
        el("label", { class: "field", for: "kdir" }, "フォルダの場所"),
        el("div", { class: "row" }, this.input,
          el("button", { class: "btn", type: "button", text: "保存", onclick: () => this.save() }), pick, this.clear),
        el("p", { class: "muted small", text: "~ はホームフォルダです。なければ空のまま「次へ」で大丈夫です。" }),
        this.msg,
      );
    },
    async save(value) {
      const path = value === undefined ? this.input.value.trim() : value;
      try {
        await api("/api/knowledge", { path });
        this.input.value = S.knowledge.value || "";
        this.msg.replaceChildren(banner("ok", S.knowledge.value ? "知識フォルダ: " + S.knowledge.value
          : "知識フォルダは使いません。"));
        return true;
      } catch (e) {
        this.msg.replaceChildren(banner("err", e.message));
        return false;
      }
    },
    async pick() {
      this.msg.replaceChildren(el("p", { class: "muted", text: "フォルダを選ぶ画面を開いています（ほかのウィンドウの後ろに出ることがあります）…" }));
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

  const MODEL_ABOUT = {
    sonnet: "バランスがよい（おすすめ）",
    opus: "いちばんよく考えます。利用上限に早く届きます",
    haiku: "いちばん速く答えます。かんたんな用事向け",
  };

  const modelStep = {
    id: "model",
    label: "モデル",
    title: "Claude のモデルを選びます",
    bubble: "考えるときに使う Claude のモデルを選ぶのだ",
    enter(body) {
      this.radios = {};
      const group = el("div", { class: "choices", role: "radiogroup", "aria-label": "Claude のモデル" });
      for (const m of S.model.choices) {
        const input = el("input", { type: "radio", name: "model", value: m,
          onchange: () => send("/api/model", { model: m }) });
        this.radios[m] = { input, card: null };
        const card = el("label", { class: "choice" }, input, el("span", { class: "name", text: m }),
          el("span", { class: "small muted", text: MODEL_ABOUT[m] || "" }));
        this.radios[m].card = card;
        group.append(card);
      }
      this.current = el("p", { class: "muted small" });
      body.append(group, this.current,
        el("p", { class: "muted small", text: "次の会話から使われます。あとから設定で変えられます。" }));
    },
    update() {
      for (const [m, r] of Object.entries(this.radios)) {
        r.input.checked = S.model.current === m;
        r.card.classList.toggle("selected", S.model.current === m);
      }
      this.current.textContent = S.model.current ? "" : "今: Claude の既定のモデル";
    },
    next: () => [true, ""],
  };

  const STAGE_TEXT = {
    install: "常駐の設定をしています…",
    wait: "起動を待っています（文字起こしのモデルを読み込むので、1 分ほどかかります）…",
  };

  const startStep = {
    id: "start",
    label: "起動",
    title: "常駐の設定をして起動します",
    bubble: "準備ができたら、ボクを起動するのだ！",
    enter(body) {
      const micLabel = (S.mic.choices.find((c) => c.selected) || {}).label || "OS の既定のマイク";
      body.append(el("dl", { class: "summary" },
        el("dt", { text: "声" }), el("dd", { text: S.voice.current_label || "" }),
        el("dt", { text: "話す速さ" }), el("dd", { text: S.voice.speed.toFixed(2) + " 倍" }),
        el("dt", { text: "マイク" }), el("dd", { text: micLabel }),
        el("dt", { text: "知識フォルダ" }), el("dd", { text: S.knowledge.value || "なし" }),
        el("dt", { text: "モデル" }), el("dd", { text: S.model.current || "Claude の既定" }),
        el("dt", { text: "設定ファイル" }), el("dd", {}, code(S.config_path))));
      this.ready = el("div");
      this.go = el("button", { class: "btn btn-primary", type: "button", text: "起動する",
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
      if (!prereqDone()) waitingFor.push("ダウンロード");
      if (!loginOk()) waitingFor.push("Claude へのログイン");
      const rkey = waitingFor.join() + "|" + JSON.stringify(S.prereq);
      if (rkey !== this.readyKey) {
        this.readyKey = rkey;
        this.ready.replaceChildren();
        if (!prereqDone()) {
          const e = S.prereq.engine;
          const m = S.prereq.model;
          this.ready.append(banner("warn",
            el("p", { text: "ダウンロードが終わると起動できます。" }),
            el("p", { class: "small", text: "声のエンジン: " + downloadText(e, true) }),
            el("p", { class: "small", text: "文字起こしのモデル: " + downloadText(m, false) })));
        } else if (!loginOk()) {
          this.ready.append(banner("err", "先に「Claude ログイン」の手順でログインを確かめてください。"));
        }
      }
      this.go.disabled = waitingFor.length > 0 || st.state === "running";
      this.go.textContent = st.state === "error" ? "もう一度試す" : "起動する";
      this.goRow.hidden = st.state === "done" || st.state === "running";
      this.bar.hidden = st.state !== "running";
      setBar(this.bar, null);
      this.progress.textContent = st.state === "running"
        ? (st.stage === "daemon_mic" ? "常駐しているほうのマイクを確かめています…" : STAGE_TEXT[st.stage] || "")
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
        this.result.append(el("p", { class: "status-ok", text: `✅ 起動しました（今: ${st.status_text}）` }));
      } else if (st.state === "done" && st.message) {
        this.result.append(banner("warn", st.message));
      }
      if (st.daemon_mic) {
        const kind = { checking: "ok", ok: "ok", silent: "warn", unknown: "warn" }[st.daemon_mic];
        const box = banner(kind, el("p", { class: "pre-line", text: st.daemon_mic_text }));
        if (st.daemon_mic === "silent" || st.daemon_mic === "unknown") {
          box.append(el("button", { class: "btn btn-small", type: "button", text: "直したので、もう一度確かめる",
            onclick: () => send("/api/daemon-mic/check") }));
        }
        this.result.append(box);
      }
    },
    next: () => [S.start.state === "done" && S.start.daemon_mic !== "checking",
      S.start.state === "done" ? "" : "起動すると次へ進めます"],
  };

  function usage() {
    const s = sys();
    const parts = [];
    if (s === "mac") {
      parts.push(el("p", { text: "画面上のメニューバーのアイコンが状態を表します（▶ 聞いています、⏳ 考え中、🔊 話し中、⏸ 一時停止）。" +
        "メニューから Pause / Resume、Conversation…（会話の表示）、Settings…（速さ・声・モデル・マイク）を開けます。" }));
    } else if (s === "windows") {
      parts.push(el("p", { text: "タスクバーの通知領域（右下）のアイコンが状態を表します。" +
        "アイコンのメニューから、一時停止や設定を開けます。" }));
    } else {
      parts.push(el("p", { text: "ログインすると自動で起動します（systemd のユーザーサービス）。" +
        "状態や操作はターミナルの zundamon-jarvisctl で見られます。" }));
    }
    parts.push(el("ul", { class: "tips" },
      el("li", {}, code("zundamon-jarvisctl toggle"), " 一時停止と再開を切り替えます"),
      el("li", {}, code("zundamon-jarvisctl watch"), " 聞き取った言葉と返事を流して表示します"),
      el("li", {}, code("zundamon-jarvisctl settings"), " 速さ・声・モデル・マイクを変えます"),
      el("li", {}, code("zundamon-jarvis setup"), " この設定をやり直します"),
      el("li", {}, code("zundamon-jarvis doctor"), " うまくいかないときの診断です")));
    return parts;
  }

  const doneStep = {
    id: "done",
    label: "完了",
    title: "設定が終わりました",
    bubble: "これからよろしくなのだ！話しかけてみてほしいのだ",
    hidden: true,
    enter(body) {
      body.append(el("p", { text: "ふつうの声で話しかけてみてください（例: 「今日は何曜日？」）。" }), ...usage());
      this.closeBtn = el("button", { class: "btn btn-primary", type: "button", text: "設定を閉じる",
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
      $("step-body").replaceChildren(el("p", { text: "設定を閉じました。このタブは閉じて大丈夫です。" }));
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
    if (!ol.childElementCount) {
      STEPS.forEach((step, i) => {
        if (step.hidden) return;
        ol.append(el("li", { "data-i": String(i) },
          el("button", { type: "button", onclick: () => go(i) },
            el("span", { class: "num", text: String(i + 1) }), el("span", { class: "label", text: step.label }))));
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
      const label = e.state !== "done" ? "声のエンジン" : "文字起こしのモデル";
      if (!mini.firstChild) mini.append(el("div"), bar());
      mini.firstChild.textContent = label + ": " + downloadText(item, item === e);
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
    $("next").textContent = step.id === "start" ? "完了へ" : "次へ";
    $("nav-hint").textContent = hint;
  }

  function render() {
    const step = STEPS[cur];
    errorBox = null;
    $("step-title").textContent = step.title;
    $("bubble").textContent = step.bubble;
    const body = $("step-body");
    body.replaceChildren();
    for (const s of STEPS) {
      s.statusKey = s.noteKey = s.resultKey = s.readyKey = s.errorKey = undefined;
    }
    step.enter(body);
    step.update();
    renderRail();
    updateNav();
    document.title = `${step.label} - ずんだもんジャーヴィスの設定`;
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
    S = state;
    if (first) {
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
    if (finished) return;
    STEPS[cur].update();
    renderRail();
    updateNav();
  }

  // ---- polling ----

  let timer = 0;

  async function poll() {
    try {
      await api("/api/state");
    } catch (e) {
      if (e.status === 403) {
        setOffline(false);
        $("step-body").replaceChildren(banner("err", "この画面の期限が切れました。ターミナルに出ている URL を開きなおしてください。"));
        return;
      }
    }
    if (!finished) timer = setTimeout(poll, STEPS[cur].fast ? 120 : 700);
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
