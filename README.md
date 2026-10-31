# zundamon-jarvis

English | [日本語](#日本語)

A hands-free voice assistant for [Claude Code](https://claude.com/claude-code), voiced by
VOICEVOX's Zundamon. You talk, it listens (speech-to-text runs locally), asks Claude Code, and
answers out loud in short spoken sentences. It runs as a background service and has a small
control surface (menu bar on macOS, a tray icon on Windows, `zundamon-jarvisctl` and an optional
bar module on Linux).

Supported environments: **linux-x86_64**, **macos-arm64** and **windows-x86_64** only.

## Requirements

- **Mac:** Apple Silicon (M1 or later) with **macOS 14 Sonoma or later** (the onnxruntime wheels need it).
  Intel Macs are not supported. **Mac support is beta.**
- **Linux:** x86_64 with an **NVIDIA CUDA GPU** (speech-to-text and VOICEVOX run on the GPU), PipeWire, systemd user session (see [Linux](#linux)).
- **Windows:** Windows 10 or 11, x64 (VOICEVOX uses the DirectML build). **Windows support is beta.**
- **Japanese only:** speech recognition, the voice, and the built-in prompts are Japanese; other languages are not supported.
- [Claude Code](https://claude.com/claude-code) installed and **logged in** (run `claude` once).
- About 8 GB of free disk (VOICEVOX engine and the speech-to-text model).
- **Headphones are recommended.** Without echo cancellation the mic would hear Zundamon, so the ears
  are muted while she speaks, and you cannot interrupt her through speakers.

## Install (Mac)

```sh
curl -fsSL https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.sh | sh
```

The installer sets up uv and the app, then runs `zundamon-jarvis setup`, which opens a local setup page in
your browser (served from 127.0.0.1 by the setup process; the terminal prints its URL). The page downloads
the VOICEVOX engine and the STT model with progress and walks through the login check, microphone (with a
level meter), headphone advice, voice choice (samples play on the computer's speakers), knowledge folder,
model and always-on service. Re-running it is safe; finished steps are skipped. `ZJ_CLI=1` keeps everything
in the terminal instead (`... | ZJ_CLI=1 sh`). A step-by-step walkthrough in Japanese is in
[docs/mac-checklist.md](docs/mac-checklist.md).

## Install (Windows)

In PowerShell (no administrator rights needed):

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.ps1 | iex"
```

The installer checks for Claude Code and 8 GB of free disk, installs uv if it is missing, installs
zundamon-jarvis with `uv tool install` (git is not needed), then runs `zundamon-jarvis setup`, which opens
the setup page in your browser; it downloads the VOICEVOX engine and the speech-to-text model and sets up
the always-on service. Re-running the same line is safe. `$env:ZJ_VERSION = 'v0.1.0'` pins a version,
`$env:ZJ_DRY_RUN = '1'` only prints what it would do.

Windows has no per-user service manager, so a small supervisor does that job: `zundamon-jarvis supervise`
(started at login by the `ZundamonJarvis` value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`,
through the windowless `zundamon-jarvisw.exe`) runs the VOICEVOX engine, the daemon and the tray icon without
console windows, and restarts them the way the Linux systemd unit does (the daemon gives up after 5 crashes
in 5 minutes; quitting the tray icon leaves it quit). `zundamon-jarvis service stop` stops everything until
the next login or `zundamon-jarvis service start`; `zundamon-jarvis service status` shows each part;
`zundamon-jarvis uninstall` removes the login entry. If it does not start at login, check that
**ZundamonJarvis** is on in Settings > Apps > Startup.

## Usage

On macOS a menu bar icon shows the state (listening, thinking, speaking, paused, daemon down). Its menu
has **Pause / Resume**, **Conversation…** (a live view of what was heard and said) and **Settings…**
(voice speed, voice, model, microphone). Quitting the menu bar app leaves the daemon running.

On Windows the tray icon (notification area; it may sit in the overflow `^` until you pin it) has the same
menu, and a left click opens the conversation. Its dot shows the state; it is greyed out while the daemon
is down. Conversation and Settings open in a Windows Terminal window (a console window if Windows Terminal
is not installed). Quitting the tray icon leaves the daemon running.

From a shell (every platform):

- `zundamon-jarvisctl toggle | pause | resume | status`
- `zundamon-jarvisctl watch` streams the conversation; `zundamon-jarvisctl settings` opens the settings window
- `zundamon-jarvisctl get` / `set speed|speaker|model|mic <value>`
- `zundamon-jarvis setup` re-opens the setup page (`setup --cli`: the same questions in the terminal);
  `zundamon-jarvis doctor` prints a diagnostic report;
  `zundamon-jarvis uninstall` removes the service and its files

## Updating

`zundamon-jarvis update` installs the newest release (`update --check` only looks; exit 1 means one is out).
On Mac and Windows the menu bar / tray also shows **Update to vX.Y.Z…** when one is out; the update runs in
the background, restarts the service, and logs to `update.log` in the log directory. A Linux git checkout is
updated with `git pull --ff-only` and `uv sync` (`zundamon-jarvis update` does both when the tree is clean).

## Configuration

`config.toml` keys (all optional; comments and unknown keys are kept when the tool edits the file):

| key | meaning |
|---|---|
| `workdir` | Directory Claude Code runs in, so its `CLAUDE.md`, memory and hooks apply. Default: your home directory. |
| `knowledge_dir` | Optional notes folder (for example `~/notes`). Given to the Claude session as an extra directory (its `CLAUDE.md` loads too), so it can read and write your notes. Unset = none. |
| `model` | `sonnet` (default), `opus` or `haiku`. Applies from the next session. |
| `speed` | VOICEVOX speed, 0.8 to 2.0 (default 1.2). |
| `speaker_id` | VOICEVOX style id (the voice). Audition with `zundamon-jarvis voices`. |
| `mic_device` | Input device: a source name or a PortAudio device index. Unset = follow the OS default input. |
| `duplex` | `auto` (default), `full` (always listening) or `half` (mic ignored while she speaks, no barge-in). `auto` means `half` only on a Mac playing through its built-in speakers (always `full` on Linux). |

| | Linux | macOS | Windows |
|---|---|---|---|
| config | `~/.config/zundamon-jarvis/config.toml` | `~/Library/Application Support/zundamon-jarvis/config.toml` | `%APPDATA%\zundamon-jarvis\config.toml` |
| data (engine, models) | `~/.local/share/zundamon-jarvis/` | `~/Library/Application Support/zundamon-jarvis/` | `%LOCALAPPDATA%\zundamon-jarvis\` |
| logs / conversation log | `~/.local/state/zundamon-jarvis/` | `~/Library/Logs/zundamon-jarvis/` | `%LOCALAPPDATA%\zundamon-jarvis\logs\` |
| service | systemd user units | LaunchAgents in `~/Library/LaunchAgents/` | `ZundamonJarvis` in `HKCU\…\CurrentVersion\Run`, starting `zundamon-jarvis supervise` |

## Linux

There is no one-line installer on Linux. You need `uv`, `claude`, an NVIDIA CUDA GPU, PipeWire (with `pactl`)
and a systemd user session. **Clone to `~/repos/zundamon-jarvis`**: the systemd unit runs
`~/repos/zundamon-jarvis/.venv/bin/zundamon-jarvis`.

```sh
mkdir -p ~/repos && git clone https://github.com/dyoshyy/zundamon-jarvis ~/repos/zundamon-jarvis
cd ~/repos/zundamon-jarvis
uv sync
uv run zundamon-jarvis setup   # setup page in the browser: downloads the engine and model, installs and starts the units
```

Without a browser, in the terminal: `uv run zundamon-jarvis engine install` (add `--cpu` for the CPU build),
`uv run zundamon-jarvis models download`, then `uv run zundamon-jarvis setup --cli`.

By hand instead of the wizard's service step: copy `deploy/systemd/*.service` to `~/.config/systemd/user/`, run
`systemctl --user daemon-reload`, then `systemctl --user enable --now voicevox zundamon-jarvis`.
Logs: `journalctl --user -u zundamon-jarvis`. `zundamon-jarvis uninstall` stops and removes the systemd units.

Optional echo cancellation (PipeWire WebRTC AEC, lets you use speakers): `deploy/install-audio.sh`,
then set `mic_device = "zundamon_jarvis_aec_source"`. Edit
`deploy/pipewire/zundamon-jarvis-echo-cancel.conf` first if you want to pin a specific microphone.

Optional Omarchy/Hyprland integration (outside this repo, in your own config): bind a key to
`zundamon-jarvisctl toggle`, add a command module to the bar that runs `zundamon-jarvisctl bar`
(click: `zundamon-jarvisctl window`, right-click: `zundamon-jarvisctl window settings`), and float the
windows with classes `zundamon-jarvis-watch` and `zundamon-jarvis-settings`.

## Privacy

- **Speech-to-text is local** (faster-whisper on Linux, mlx-whisper on Mac). Your audio never leaves the machine.
- What is sent to Claude: the **text** of what you said, and whatever Claude Code reads or runs while
  answering you in `workdir`, exactly as in a normal Claude Code session (under your own Claude account).
  Zundamon's text-to-speech runs in the local VOICEVOX engine.
- The conversation log stays on your machine (see the table above) and old days are deleted after 14 days.
- `zundamon-jarvis doctor` redacts its report before printing: speech and conversation text, user and host
  names, home and `knowledge_dir` paths, and e-mail addresses are removed, so it is safe to paste into an issue.

## Voice confirmation policy

Most things run without asking: reading and writing files, running commands, looking things up. Before
these, Zundamon asks a spoken yes/no question: sending e-mail or messages to people, creating, changing
or deleting calendar events, deleting files or data, anything involving money, `sudo`, and force pushes.
Answering "no" (or anything unclear) cancels the action.

## Troubleshooting

- **No reaction / "mic unavailable" (Mac):** System Settings > Privacy & Security > Microphone, turn on
  **python3.12** (the daemon's name) as well as your terminal. Then `zundamon-jarvisctl status`.
- **No reaction / "mic unavailable" (Windows):** Settings > Privacy & security > Microphone, turn on
  **Let desktop apps access your microphone**. Then `zundamon-jarvisctl status`.
- **Windows: nothing runs after login:** `zundamon-jarvis service status`, and the logs in
  `%LOCALAPPDATA%\zundamon-jarvis\logs\` (`supervisor.log`, `daemon.log`, `engine.log`, `tray.log`).
- **Not logged in:** run `claude` in a terminal and log in.
- **She hears herself:** use headphones, or on Linux install the optional echo cancellation.
- **Anything else:** run `zundamon-jarvis doctor` and attach its (redacted) output to an issue.

## Credits and terms

- Voice: **[VOICEVOX](https://voicevox.hiroshiba.jp/)** engine and the character **Zundamon (ずんだもん)**.
  Read the [VOICEVOX terms of use](https://voicevox.hiroshiba.jp/term/). **Each character has its own
  terms of use** as well. If you publish audio made with this tool (videos, streams, posts), you must
  credit it, for example **「VOICEVOX:ずんだもん」**. This repository does not include VOICEVOX or any voice data;
  the installer downloads the engine from its official release.
- Voice activity detection: the [Silero VAD](https://github.com/snakers4/silero-vad) model
  (`silero_vad_v6.onnx`, MIT License; text in `src/zundamon_jarvis/assets/LICENSE-silero`).
- Speech-to-text models: Whisper (OpenAI, MIT) through faster-whisper / mlx-whisper.

## License

[MIT](LICENSE), copyright (c) 2026 dyoshyy. The licence covers this code only, not VOICEVOX, its voices
or the Zundamon character.

---

# 日本語

[Claude Code](https://claude.com/claude-code) を声で操作するための、VOICEVOX ずんだもんの音声アシスタントです。
話しかけると（文字起こしはローカルで実行）、Claude Code に聞いて、短い話し言葉で答えてくれます。
常駐サービスとして動き、Mac ではメニューバー、Windows では通知領域（タスクトレイ）のアイコン、Linux では `zundamon-jarvisctl`（と任意のバーモジュール）で操作します。

対応環境は **linux-x86_64**、**macos-arm64**、**windows-x86_64** のみです。

## 必要なもの

- **Mac:** Apple シリコン（M1 以降）と **macOS 14 Sonoma 以降**（onnxruntime の都合）。Intel Mac は非対応。**Mac 対応はベータ版です。**
- **Linux:** x86_64 と **NVIDIA の CUDA GPU**（文字起こしと VOICEVOX は GPU で動かします）、PipeWire、systemd のユーザーセッション（[Linux](#linux) 参照）。
- **Windows:** Windows 10 / 11 の x64（VOICEVOX は DirectML 版を使います）。**Windows 対応はベータ版です。**
- **日本語専用:** 音声認識・声・内蔵プロンプトはすべて日本語です。他の言語には対応していません。
- [Claude Code](https://claude.com/claude-code) をインストールし、**ログイン済み**であること（一度 `claude` を起動）。
- ディスクの空き 8 GB 程度（VOICEVOX エンジンと文字起こしモデル）。
- **ヘッドホン推奨。** スピーカーだとずんだもんの声をマイクが拾うので、話している間は聞き取りを止めます（割り込めません）。

## インストール（Mac）

```sh
curl -fsSL https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.sh | sh
```

uv と本体を入れたあと、`zundamon-jarvis setup` がブラウザで設定画面を開きます。画面の中で VOICEVOX エンジンと
文字起こしモデルを（進み具合を見せながら）ダウンロードし、ログイン確認、マイク（音のメーター付き）、声選び（試聴は
パソコンのスピーカーから）、知識フォルダ、モデル、常駐設定まで進みます。途中で止まっても同じ 1 行で続きから進みます。
ターミナルだけで設定したいときは `ZJ_CLI=1` を付けます（`… | ZJ_CLI=1 sh`）。
詳しい手順は [docs/mac-checklist.md](docs/mac-checklist.md) を見てください。

## インストール（Windows）

PowerShell で（管理者権限は要りません）:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.ps1 | iex"
```

Claude Code とディスクの空き（8 GB）を確認し、uv が無ければ入れて、`uv tool install` で本体を入れ（git は不要）、
`zundamon-jarvis setup` を起動します。設定はブラウザで開き、VOICEVOX エンジンと文字起こしモデルの取得、常駐の設定までそこで済みます。
同じ 1 行を何度実行しても大丈夫です。`$env:ZJ_VERSION = 'v0.1.0'` で版を固定、`$env:ZJ_DRY_RUN = '1'` で実行せず表示だけになります。

Windows には systemd や launchd にあたるものが無いので、`zundamon-jarvis supervise` が代わりを務めます。ログイン時に
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` の `ZundamonJarvis` から（ウィンドウの出ない `zundamon-jarvisw.exe` で）起動し、
VOICEVOX エンジン・デーモン・トレイアイコンをコンソール窓なしで動かし、落ちたら起こし直します（5 分に 5 回落ちたらあきらめます。トレイを終了したときは起こしません）。
`zundamon-jarvis service stop` で次のログイン（か `zundamon-jarvis service start`）まで止まり、`zundamon-jarvis service status` で各部の状態が見られます。
ログイン時に起動しないときは、設定 > アプリ > スタートアップ で **ZundamonJarvis** がオンになっているか確認してください。

## 使い方

Mac ではメニューバーのアイコンが状態を表し、メニューから **Pause / Resume**、**Conversation…**（会話のライブ表示）、
**Settings…**（速度・声・モデル・マイク）を開けます。メニューバーを終了しても常駐は止まりません。
Windows ではトレイアイコン（隠れているときは `^` の中）が同じメニューを持ち、左クリックで会話を開きます。
Conversation と Settings は Windows Terminal の窓で開きます。トレイを終了しても常駐は止まりません。
シェルからは `zundamon-jarvisctl toggle|pause|resume|status|watch|settings|get|set`、`zundamon-jarvis setup|doctor|uninstall` が使えます
（`zundamon-jarvis setup --cli` で、設定をターミナルで行えます）。

## 更新

`zundamon-jarvis update` で最新の版を入れます（`update --check` は確かめるだけ。更新があると終了コード 1）。
Mac と Windows では、新しい版が出るとメニューバー / トレイに **vX.Y.Z に更新…** が出ます。更新は裏で進み、常駐を起動し直し、ログはログの置き場所の `update.log` です。
Linux の git の clone は `git pull --ff-only` と `uv sync` で更新します（変更の無い状態なら `zundamon-jarvis update` が両方行います）。

## 設定

`config.toml` のキー（すべて任意）: `workdir`（Claude Code を動かすディレクトリ。既定はホーム）、
`knowledge_dir`（メモのフォルダ。例 `~/notes`。Claude に追加ディレクトリとして渡す。未設定なら使わない）、`model`（`sonnet`/`opus`/`haiku`）、
`speed`（0.8〜2.0、既定 1.2）、`speaker_id`（声）、`mic_device`（入力デバイス。未設定ならOS既定）、`duplex`（`auto`/`full`/`half`。`half` は話している間マイクを無視）。

置き場所は Linux が `~/.config/zundamon-jarvis/config.toml`、Mac が `~/Library/Application Support/zundamon-jarvis/config.toml`。
Windows は `%APPDATA%\zundamon-jarvis\config.toml`。
ログは Linux が `~/.local/state/zundamon-jarvis/`、Mac が `~/Library/Logs/zundamon-jarvis/`、Windows が `%LOCALAPPDATA%\zundamon-jarvis\logs\`。

## Linux

1 行インストーラはありません。`uv`、`claude`、PipeWire（`pactl`）、systemd ユーザーセッションを用意し、
`~/repos/zundamon-jarvis` に clone し、上の English の [Linux](#linux) の手順（`uv sync` のあと `setup` でブラウザの設定画面。
ターミナルで行うなら `engine install`、`models download`、`setup --cli`）で入れます。
`zundamon-jarvis uninstall` は systemd のユニットも削除します。Omarchy/Hyprland との連携とエコーキャンセルは任意です。

## プライバシー

- **文字起こしはローカル**で行い、音声は外に出ません。
- Claude に送られるのは、話した内容の**テキスト**と、`workdir` で Claude Code が読んだり実行したりした内容です（通常の Claude Code と同じ）。
- 会話ログは手元にだけ保存され、14 日で消えます。
- `zundamon-jarvis doctor` は、会話内容・ユーザー名・ホスト名・パス・メールアドレスを伏せて出力するので、そのまま Issue に貼れます。

## 声での確認ルール

ファイルの読み書きやコマンド実行は確認なしで動きます。次のときだけ声で「はい／いいえ」を聞きます:
メールやメッセージの送信、カレンダーの作成・変更・削除、ファイルやデータの削除、お金に関わること、`sudo`、force push。
「いいえ」や聞き取れない返事は中止になります。

## うまくいかないとき

- Mac で反応しない: システム設定 > プライバシーとセキュリティ > マイク で **python3.12** をオンにする。
- Windows で反応しない: 設定 > プライバシーとセキュリティ > マイク で「**デスクトップ アプリがマイクにアクセスできるようにする**」をオンにする。
  ログイン後に動いていないときは `zundamon-jarvis service status` と `%LOCALAPPDATA%\zundamon-jarvis\logs\` のログを見る。
- ログインしていない: ターミナルで `claude` を実行してログインする。
- 自分の声を拾う: ヘッドホンを使う（Linux は任意のエコーキャンセルも可）。
- それ以外: `zundamon-jarvis doctor` の出力を添えて Issue へ。

## クレジットと利用規約

- 音声は **[VOICEVOX](https://voicevox.hiroshiba.jp/)** と キャラクター **ずんだもん** です。
  [VOICEVOX 利用規約](https://voicevox.hiroshiba.jp/term/)を確認してください。**キャラクターごとに個別の利用規約**があります。
  生成した音声を公開する場合は、**「VOICEVOX:ずんだもん」** のようにクレジット表記が必要です。
  このリポジトリに VOICEVOX や音声データは含まれず、インストーラが公式のリリースから取得します。
- 音声区間検出に [Silero VAD](https://github.com/snakers4/silero-vad)（MIT License）を同梱しています。
- ライセンスは [MIT](LICENSE)（Copyright (c) 2026 dyoshyy）。VOICEVOX・音声・ずんだもんには及びません。
