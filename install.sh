#!/bin/sh
# zundamon-jarvis one-line installer (Apple Silicon macOS 14+)
#   curl -fsSL https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.sh | sh
# 環境変数: ZJ_VERSION=v0.1.0 (版を固定) / ZJ_DRY_RUN=1 (実行せず表示だけ)
#           ZJ_CLI=1 (設定をブラウザでなくターミナルで行う。エンジンとモデルもここで取得する)
#           ZJ_LANG=ja|en (表示の言語。既定は LC_ALL / LC_MESSAGES / LANG が ja なら日本語、ほかは英語)
set -eu

OWNER_REPO="dyoshyy/zundamon-jarvis"
ZJ_VERSION="${ZJ_VERSION:-}"
ZJ_DRY_RUN="${ZJ_DRY_RUN:-0}"
ZJ_CLI="${ZJ_CLI:-0}"
ZJ_TTY="${ZJ_TTY:-/dev/tty}"
MIN_FREE_KB=$((8 * 1024 * 1024))
CURRENT_STEP=""

# 表示の言語: ZJ_LANG、なければ最初に値のあるロケール変数（zundamon_jarvis.i18n と同じ順）
if [ -z "${ZJ_LANG:-}" ]; then
  ZJ_LANG=en
  for _v in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
    if [ -n "$_v" ]; then
      case "$_v" in ja*) ZJ_LANG=ja ;; esac
      break
    fi
  done
fi

# msg <日本語> <English>: 表示の言語の方を出す（改行なし。$(msg ...) で使う）
msg() {
  if [ "$ZJ_LANG" = ja ]; then printf '%s' "$1"; else printf '%s' "$2"; fi
}

say() { printf '%s\n' "$*"; }
step() { CURRENT_STEP="$1"; say "==> $1"; }
die() { say "$*" >&2; exit 1; }

fail_step() {
  say "==> $(msg "失敗しました" "Failed"): ${CURRENT_STEP}" >&2
  say "    $(msg "原因を直したあと、同じコマンドをもう一度実行すれば、続きから進みます。" \
    "Fix the cause and run the same command again: it picks up where it stopped.")" >&2
  exit 1
}

# run <command...>: dry-run なら表示だけ。失敗したら段の名前を出して止まる。
# 子プロセスの stdin は /dev/null（curl | sh で、残りのスクリプトを読まれないように）。
run() {
  if [ "$ZJ_DRY_RUN" = 1 ]; then
    say "    [dry-run] $*"
    return 0
  fi
  "$@" </dev/null || fail_step
}

# 全体を関数に包み、最後の行で呼ぶ。curl | sh が途中で切れても、半端なスクリプトは走らない。
main() {
  ORIG_PATH="$PATH"
  # ネイティブ版の Claude Code は ~/.local/bin に入る
  export PATH="$HOME/.local/bin:$PATH"

  # ① 環境の確認
  step "$(msg "環境を確認しています" "Checking your system")"
  OS="$(uname -s)"
  ARCH="$(uname -m)"
  if [ "$OS" = Linux ]; then
    die "$(msg "Linux では、この 1 行インストーラは使いません。README の Linux 手順に従ってください。" \
      "This one-line installer is not for Linux. Follow the Linux steps in the README:")
    https://github.com/dyoshyy/zundamon-jarvis#linux"
  fi
  if [ "$OS" = Darwin ] && [ "$ARCH" != arm64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
    die "$(msg "ターミナルが Rosetta で動いています。Rosetta を使わずに開き直してから、もう一度実行してください。" \
      "Your terminal is running under Rosetta. Reopen it without Rosetta and run this again.")"
  fi
  if [ "$OS" != Darwin ] || [ "$ARCH" != arm64 ]; then
    die "$(msg "対応していない環境です（$OS / $ARCH）。Apple Silicon の Mac（macOS 14 Sonoma 以降）が必要です。" \
      "Not supported here ($OS / $ARCH): you need an Apple Silicon Mac with macOS 14 Sonoma or later.")"
  fi
  MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null || echo 0)"
  MACOS_MAJOR="${MACOS_VERSION%%.*}"
  case "$MACOS_MAJOR" in ''|*[!0-9]*) MACOS_MAJOR=0 ;; esac
  if [ "$MACOS_MAJOR" -lt 14 ]; then
    die "$(msg "macOS $MACOS_VERSION では動きません。Apple Silicon の Mac と macOS 14 Sonoma 以降が必要です。" \
      "macOS $MACOS_VERSION is too old: you need an Apple Silicon Mac with macOS 14 Sonoma or later.")"
  fi

  # git（macOS の /usr/bin/git は、未導入だと案内ダイアログを出すだけの代役）
  if ! command -v git >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
    die "$(msg "git が使えません。xcode-select --install を実行してから、もう一度このコマンドを実行してください。" \
      "git is not available. Run xcode-select --install, then run this command again.")"
  fi

  # ② claude
  if ! command -v claude >/dev/null 2>&1; then
    die "$(msg "claude コマンドが見つかりません。先に Claude Code をインストールしてログインしてから、もう一度実行してください。" \
      "The claude command was not found. Install Claude Code and log in first, then run this again.")
    https://claude.com/claude-code"
  fi

  # ③ ディスクの空き
  FREE_KB="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  case "$FREE_KB" in ''|*[!0-9]*) FREE_KB=0 ;; esac
  if [ "$FREE_KB" -lt "$MIN_FREE_KB" ]; then
    die "$(msg "ディスクの空きが足りません（$((FREE_KB / 1024 / 1024)) GB）。モデルと音声エンジンで 8 GB 以上の空きが必要です。空けてからもう一度実行してください。" \
      "Not enough free disk space ($((FREE_KB / 1024 / 1024)) GB). The models and the voice engine need 8 GB or more. Free some up and run this again.")"
  fi

  # 版の決定
  if [ -z "$ZJ_VERSION" ]; then
    if [ "$ZJ_DRY_RUN" = 1 ]; then
      ZJ_VERSION="<latest tag>"
    else
      step "$(msg "最新の版を調べています" "Looking up the latest version")"
      ZJ_VERSION="$(curl -fsSL "https://api.github.com/repos/$OWNER_REPO/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)" || ZJ_VERSION=""
      if [ -z "$ZJ_VERSION" ]; then
        die "$(msg "最新の版を取得できませんでした（リリースが無いか、GitHub の問い合わせ回数の上限です）。
    版を指定してもう一度実行してください。例: ZJ_VERSION=v0.1.0 sh install.sh" \
          "Could not get the latest version (no release yet, or GitHub's rate limit).
    Run again with a version, e.g. ZJ_VERSION=v0.1.0 sh install.sh")"
      fi
    fi
  fi
  say "    $(msg "版" "Version"): $ZJ_VERSION"

  # ④ uv
  step "$(msg "uv を確認しています" "Checking uv")"
  if command -v uv >/dev/null 2>&1; then
    say "    $(msg "uv は入っています" "uv is installed")"
  else
    say "    $(msg "uv を入れます" "Installing uv")"
    if [ "$ZJ_DRY_RUN" = 1 ]; then
      say "    [dry-run] curl -LsSf https://astral.sh/uv/install.sh | sh"
    else
      curl -LsSf https://astral.sh/uv/install.sh | sh || fail_step
      command -v uv >/dev/null 2>&1 || fail_step
    fi
  fi

  # uv tool の bin ディレクトリを、このスクリプトの PATH に入れる
  UV_BIN=""
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(uv tool dir --bin 2>/dev/null || true)"
  fi
  [ -n "$UV_BIN" ] || UV_BIN="$HOME/.local/bin"
  export PATH="$UV_BIN:$PATH"

  # ⑤ 本体
  step "$(msg "zundamon-jarvis を入れます" "Installing zundamon-jarvis")"
  WANT="${ZJ_VERSION#v}"
  HAVE=""
  if [ "$ZJ_DRY_RUN" != 1 ] && command -v zundamon-jarvis >/dev/null 2>&1; then
    HAVE="$(zundamon-jarvis --version 2>/dev/null </dev/null | awk '{print $NF}' | head -n 1 || true)"
  fi
  if [ -n "$HAVE" ] && [ "$HAVE" = "$WANT" ]; then
    say "    $(msg "すでに $HAVE が入っています。飛ばします" "$HAVE is already installed. Skipping")"
  else
    [ -z "$HAVE" ] || say "    $(msg "$HAVE から $ZJ_VERSION に入れ直します" "Replacing $HAVE with $ZJ_VERSION")"
    run uv tool install --force --python 3.12 "git+https://github.com/$OWNER_REPO@$ZJ_VERSION"
  fi
  if [ "$ZJ_DRY_RUN" != 1 ]; then
    command -v zundamon-jarvis >/dev/null 2>&1 || fail_step
    case ":$ORIG_PATH:" in
      *":$UV_BIN:"*) ;;
      *)
        say "    $(msg "注意: $UV_BIN がシェルの PATH に入っていません。" "Note: $UV_BIN is not on your shell's PATH.")"
        say "    $(msg "このあと uv tool update-shell を実行して、ターミナルを開き直してください。" \
          "Run uv tool update-shell afterwards, then reopen the terminal.")"
        ;;
    esac
  fi

  # ⑥ 設定。ふつうはブラウザの設定画面（声のエンジンとモデルのダウンロードも、そこで進み具合を見せながら行う）。
  # ブラウザが開かないときは、端末に出る URL を開く。
  if [ "$ZJ_CLI" != 1 ]; then
    step "$(msg "設定の画面を開きます（ブラウザで続けてください）" "Opening the setup page (continue in your browser)")"
    if [ "$ZJ_DRY_RUN" = 1 ]; then
      say "    [dry-run] zundamon-jarvis setup"
    elif ! zundamon-jarvis setup </dev/null; then
      say "==> $(msg "設定は終わっていません。ターミナルで zundamon-jarvis setup を実行すると、続きから設定できます。" \
        "Setup is not finished. Run zundamon-jarvis setup in a terminal to pick up where you left off.")" >&2
      exit 1
    fi
    say "==> $(msg "完了しました" "Done")"
    return 0
  fi

  # ZJ_CLI=1: これまでどおりターミナルで。エンジンとモデルを先に取得する（済んでいれば何もせず戻る）
  step "$(msg "音声エンジンを入れます（数 GB あります）" "Installing the voice engine (a few GB)")"
  run zundamon-jarvis engine install

  step "$(msg "文字起こしのモデルを取得します（数 GB あります）" "Downloading the speech recognition model (a few GB)")"
  run zundamon-jarvis models download

  # 対話。curl | sh では stdin がパイプなので端末を直接つなぐ
  step "$(msg "設定を始めます" "Starting setup")"
  if [ "$ZJ_DRY_RUN" = 1 ]; then
    say "    [dry-run] zundamon-jarvis setup --cli < $ZJ_TTY"
  elif (: < "$ZJ_TTY") 2>/dev/null; then
    zundamon-jarvis setup --cli < "$ZJ_TTY" || fail_step
  else
    say "    $(msg "端末から入力を受け取れないので、設定は自動では始めません。" "No terminal to read your answers from, so setup won't start by itself.")"
    say "    $(msg "ターミナルで次のコマンドを実行してください:" "Run this in a terminal:")"
    say "        zundamon-jarvis setup --cli"
    exit 0
  fi

  say "==> $(msg "完了しました" "Done")"
}

main "$@"
