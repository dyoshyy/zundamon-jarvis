#!/bin/sh
# zundamon-jarvis one-line installer (Apple Silicon macOS 14+)
#   curl -fsSL https://raw.githubusercontent.com/dyoshyy/zundamon-jarvis/main/install.sh | sh
# 環境変数: ZJ_VERSION=v0.1.0 (版を固定) / ZJ_DRY_RUN=1 (実行せず表示だけ)
set -eu

OWNER_REPO="dyoshyy/zundamon-jarvis"
ZJ_VERSION="${ZJ_VERSION:-}"
ZJ_DRY_RUN="${ZJ_DRY_RUN:-0}"
ZJ_TTY="${ZJ_TTY:-/dev/tty}"
MIN_FREE_KB=$((8 * 1024 * 1024))
CURRENT_STEP=""

say() { printf '%s\n' "$*"; }
step() { CURRENT_STEP="$1"; say "==> $1"; }
die() { say "$*" >&2; exit 1; }

fail_step() {
  say "==> 失敗しました: ${CURRENT_STEP}" >&2
  say "    原因を直したあと、同じコマンドをもう一度実行すれば、続きから進みます。" >&2
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
  step "環境を確認しています"
  OS="$(uname -s)"
  ARCH="$(uname -m)"
  if [ "$OS" = Linux ]; then
    die "Linux では、この 1 行インストーラは使いません。README の Linux 手順に従ってください。
    https://github.com/dyoshyy/zundamon-jarvis#linux"
  fi
  if [ "$OS" = Darwin ] && [ "$ARCH" != arm64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
    die "ターミナルが Rosetta で動いています。Rosetta を使わずに開き直してから、もう一度実行してください。"
  fi
  if [ "$OS" != Darwin ] || [ "$ARCH" != arm64 ]; then
    die "対応していない環境です（$OS / $ARCH）。Apple Silicon の Mac（macOS 14 Sonoma 以降）が必要です。"
  fi
  MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null || echo 0)"
  MACOS_MAJOR="${MACOS_VERSION%%.*}"
  case "$MACOS_MAJOR" in ''|*[!0-9]*) MACOS_MAJOR=0 ;; esac
  if [ "$MACOS_MAJOR" -lt 14 ]; then
    die "macOS $MACOS_VERSION では動きません。Apple Silicon の Mac と macOS 14 Sonoma 以降が必要です。"
  fi

  # git（macOS の /usr/bin/git は、未導入だと案内ダイアログを出すだけの代役）
  if ! command -v git >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
    die "git が使えません。xcode-select --install を実行してから、もう一度このコマンドを実行してください。"
  fi

  # ② claude
  if ! command -v claude >/dev/null 2>&1; then
    die "claude コマンドが見つかりません。先に Claude Code をインストールしてログインしてから、もう一度実行してください。
    https://claude.com/claude-code"
  fi

  # ③ ディスクの空き
  FREE_KB="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  case "$FREE_KB" in ''|*[!0-9]*) FREE_KB=0 ;; esac
  if [ "$FREE_KB" -lt "$MIN_FREE_KB" ]; then
    die "ディスクの空きが足りません（$((FREE_KB / 1024 / 1024)) GB）。モデルと音声エンジンで 8 GB 以上の空きが必要です。空けてからもう一度実行してください。"
  fi

  # 版の決定
  if [ -z "$ZJ_VERSION" ]; then
    if [ "$ZJ_DRY_RUN" = 1 ]; then
      ZJ_VERSION="<latest tag>"
    else
      step "最新の版を調べています"
      ZJ_VERSION="$(curl -fsSL "https://api.github.com/repos/$OWNER_REPO/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)" || ZJ_VERSION=""
      if [ -z "$ZJ_VERSION" ]; then
        die "最新の版を取得できませんでした（リリースが無いか、GitHub の問い合わせ回数の上限です）。
    版を指定してもう一度実行してください。例: ZJ_VERSION=v0.1.0 sh install.sh"
      fi
    fi
  fi
  say "    版: $ZJ_VERSION"

  # ④ uv
  step "uv を確認しています"
  if command -v uv >/dev/null 2>&1; then
    say "    uv は入っています"
  else
    say "    uv を入れます"
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
  step "zundamon-jarvis を入れます"
  WANT="${ZJ_VERSION#v}"
  HAVE=""
  if [ "$ZJ_DRY_RUN" != 1 ] && command -v zundamon-jarvis >/dev/null 2>&1; then
    HAVE="$(zundamon-jarvis --version 2>/dev/null </dev/null | awk '{print $NF}' | head -n 1 || true)"
  fi
  if [ -n "$HAVE" ] && [ "$HAVE" = "$WANT" ]; then
    say "    すでに $HAVE が入っています。飛ばします"
  else
    [ -z "$HAVE" ] || say "    $HAVE から $ZJ_VERSION に入れ直します"
    run uv tool install --force --python 3.12 "git+https://github.com/$OWNER_REPO@$ZJ_VERSION"
  fi
  if [ "$ZJ_DRY_RUN" != 1 ]; then
    command -v zundamon-jarvis >/dev/null 2>&1 || fail_step
    case ":$ORIG_PATH:" in
      *":$UV_BIN:"*) ;;
      *)
        say "    注意: $UV_BIN がシェルの PATH に入っていません。"
        say "    このあと uv tool update-shell を実行して、ターミナルを開き直してください。"
        ;;
    esac
  fi

  # ⑥⑦ 音声エンジンとモデル（どちらも、済んでいれば何もせず戻る）
  step "音声エンジンを入れます（数 GB あります）"
  run zundamon-jarvis engine install

  step "文字起こしのモデルを取得します（数 GB あります）"
  run zundamon-jarvis models download

  # ⑧ 設定（対話。curl | sh では stdin がパイプなので端末を直接つなぐ）
  step "設定を始めます"
  if [ "$ZJ_DRY_RUN" = 1 ]; then
    say "    [dry-run] zundamon-jarvis setup < $ZJ_TTY"
  elif (: < "$ZJ_TTY") 2>/dev/null; then
    zundamon-jarvis setup < "$ZJ_TTY" || fail_step
  else
    say "    端末から入力を受け取れないので、設定は自動では始めません。"
    say "    ターミナルで次のコマンドを実行してください:"
    say "        zundamon-jarvis setup"
    exit 0
  fi

  say "==> 完了しました"
}

main "$@"
