# zundamon-jarvis

English | [日本語](#日本語)

A hands-free voice assistant for [Claude Code](https://claude.com/claude-code), voiced by VOICEVOX's Zundamon.
You talk, it transcribes locally, asks Claude Code, and answers out loud.

- **Runs on:** macOS 14+ on Apple Silicon (beta), Windows 10/11 x64 (beta), Linux x86_64 with an NVIDIA GPU.
- **Needs:** [Claude Code](https://claude.com/claude-code), logged in (run `claude` once), and about 8 GB of free disk.
- **Language:** it listens and speaks in Japanese. The screens are in Japanese or English.
- **Headphones recommended:** through speakers, the mic would hear her, so you can't interrupt her.

## Install

### Before you install

The installer stops if the `claude` command isn't found. The Claude desktop app alone doesn't provide it,
so install the Claude Code CLI first:

- **Mac:** `curl -fsSL https://claude.ai/install.sh | bash`
- **Windows:** `irm https://claude.ai/install.ps1 | iex` (PowerShell)

If the CLI installer says `~/.local/bin` (Windows: `%USERPROFILE%\.local\bin`) is not on your PATH, add it and
reopen the terminal. Then run `claude` once and log in. `claude --version` should work before you go on.

**Mac**

```sh
curl -fsSL https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.sh | sh
```

**Windows** (PowerShell, no admin rights needed)

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.ps1 | iex"
```

The installer opens the setup window. It downloads the voice engine and the speech model, then walks you
through the login check, mic, voice, notes folder and model, and starts the background service. Running
the same line again is safe. To run setup again later, use `zundamon-jarvis setup` (`--cli` runs it in the terminal).

## Linux

Clone to `~/repos/zundamon-jarvis` (the systemd unit runs from there). You need `uv`, PipeWire and a systemd user session.

```sh
git clone https://github.com/dyoshyy/zundamon-jarvis ~/repos/zundamon-jarvis
cd ~/repos/zundamon-jarvis && uv sync && uv run zundamon-jarvis setup
```

Echo cancellation (so you can use speakers) and the Hyprland bar module are optional; see [docs/reference.md](docs/reference.md).

## Use

- **Mac:** the menu bar icon. **Windows:** the tray icon. Both offer Pause/Resume, Conversation and Settings.
- **Any OS:**
  - `zundamon-jarvisctl toggle | status | watch | settings`
  - `zundamon-jarvis doctor` gives a diagnostic report with personal data removed.
- **Update:** `zundamon-jarvis update`. On Mac and Windows the menu also offers it when a new version is out.
- **Display language:** set it on the setup page or in Settings, or with `language = "ja" | "en"` in `config.toml`.
  If it isn't set, the OS language is used.

Before sending messages or mail, changing your calendar, deleting things, spending money, `sudo` or a force
push, she asks you out loud first. Anything other than a clear "yes" cancels.

## Privacy

- Speech-to-text runs locally. Your audio never leaves the machine.
- Claude only gets the text of what you said. It works the same as a normal Claude Code session in your `workdir`.
- The conversation log stays on your machine and is deleted after 14 days.

## Credits and license

- Voice: [VOICEVOX](https://voicevox.hiroshiba.jp/) and **Zundamon (ずんだもん)**. Follow the
  [VOICEVOX terms](https://voicevox.hiroshiba.jp/term/) and each character's own terms. Published audio needs a
  credit such as **「VOICEVOX:ずんだもん」**. VOICEVOX is not bundled; the installer downloads the official release.
- VAD: [Silero VAD](https://github.com/snakers4/silero-vad) (MIT). Speech-to-text: Whisper via faster-whisper / mlx-whisper.
- [MIT](LICENSE) © 2026 dyoshyy. The license covers this code only, not VOICEVOX, its voices or the character.

Settings, file locations, the Windows service and troubleshooting are in [docs/reference.md](docs/reference.md).

---

# 日本語

[Claude Code](https://claude.com/claude-code) を声で使える、VOICEVOX ずんだもんの音声アシスタントです。
話しかけると、手元で文字にして Claude Code に聞き、声で答えます。

- **対応環境:** macOS 14 以降の Apple シリコン Mac（ベータ）、Windows 10 / 11 の x64（ベータ）、NVIDIA GPU のある Linux x86_64。
- **必要なもの:** [Claude Code](https://claude.com/claude-code) にログイン済みであること（`claude` を一度起動）と、8 GB ほどの空き容量。
- **言語:** 聞き取りと話す声は日本語です。画面の表示は日本語と英語を選べます。
- **ヘッドホン推奨:** スピーカーだと声をマイクが拾うので、話している途中で割り込めません。

## インストール

### インストールの前に

`claude` コマンドが見つからないと、インストーラは止まります。Claude のデスクトップアプリだけでは `claude` コマンドは入らないので、
先に Claude Code の CLI を入れてください。

- **Mac:** `curl -fsSL https://claude.ai/install.sh | bash`
- **Windows:** `irm https://claude.ai/install.ps1 | iex`（PowerShell で実行）

CLI のインストーラが「`~/.local/bin`（Windows は `%USERPROFILE%\.local\bin`）が PATH に入っていない」と言ったら、PATH に足してからターミナルを開き直します。
そのあと `claude` を一度起動してログインします。`claude --version` が動けば準備完了です。

上の Install にあるコマンドを、Mac はターミナル、Windows は PowerShell で実行します。

すると設定画面が開き、次のことをこの順で行います。

1. 声のエンジンと文字起こしのモデルをダウンロードする
2. ログインを確認する
3. マイク、声、メモのフォルダ、モデルを選ぶ
4. 常駐を始める

何度実行しても大丈夫です。あとから設定し直すときは `zundamon-jarvis setup` を使います（`--cli` を付けるとターミナルで設定します）。

Linux では、上の Linux の手順どおりに `~/repos/zundamon-jarvis` に clone して `uv sync` と `setup` を実行します。

## 使い方

- **Mac:** メニューバーのアイコン。**Windows:** タスクトレイのアイコン。どちらも一時停止、会話の表示、設定ができます。
- **どの OS でも:**
  - `zundamon-jarvisctl toggle | status | watch | settings`
  - `zundamon-jarvis doctor` は診断を出力します。個人情報は伏せてあります。
- **更新:** `zundamon-jarvis update`。Mac と Windows では、新しい版が出るとメニューにも出ます。
- **表示言語:** 設定画面か設定ウィンドウで選びます。`config.toml` の `language = "ja" | "en"` でも指定できます。
  指定しないときは OS の言語になります。

メッセージやメールの送信、カレンダーの変更、削除、お金のこと、`sudo`、force push の前には、声で確認します。
はっきり「はい」と答えたとき以外は中止します。

## プライバシー

- 文字起こしは手元で行います。音声は外に出ません。
- Claude に送られるのは、話した内容のテキストだけです。`workdir` で動く、普通の Claude Code と同じです。
- 会話ログは手元にだけ残り、14 日で消えます。

## クレジット

音声は [VOICEVOX](https://voicevox.hiroshiba.jp/) とずんだもんです。[利用規約](https://voicevox.hiroshiba.jp/term/)とキャラクターごとの規約に従ってください。
音声を公開するときは「VOICEVOX:ずんだもん」のように表記してください。ライセンスは [MIT](LICENSE) です。

設定の一覧、ファイルの置き場所、Windows の常駐の仕組み、うまくいかないときの対処は [docs/reference.md](docs/reference.md) にあります。
