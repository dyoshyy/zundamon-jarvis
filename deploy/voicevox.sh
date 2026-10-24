#!/usr/bin/env bash
# Install the VOICEVOX engine (Linux NVIDIA build) and run it as a systemd user service.
# Usage: deploy/voicevox.sh [cpu]   (cpu = use the CPU build instead)
set -euo pipefail

FLAVOR="${1:-nvidia}"
DEST="$HOME/.local/share/zundamon-jarvis/voicevox_engine"
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

# Pre-rename installs live in ~/.local/share/jarvis: move them (no re-download) and leave a
# compat symlink behind. The running engine is stopped first.
LEGACY="$HOME/.local/share/jarvis"
NEW_ROOT="$(dirname "$DEST")"
MOVED=0
if [ ! -e "$DEST" ] && [ ! -L "$LEGACY" ] && [ -x "$LEGACY/voicevox_engine/run" ]; then
  echo "moving the existing install: $LEGACY -> $NEW_ROOT"
  systemctl --user stop voicevox.service 2>/dev/null || true
  if [ -e "$NEW_ROOT" ]; then
    mv "$LEGACY/voicevox_engine" "$DEST"
  else
    mv "$LEGACY" "$NEW_ROOT"
    ln -s "$(basename "$NEW_ROOT")" "$LEGACY"
  fi
  MOVED=1
fi

# Pinned, checksummed download + extraction lives in the Python CLI (src/zundamon_jarvis/engine.py).
# Only needed when there is no engine yet.
if [ ! -x "$DEST/run" ]; then
  ZJ="$HERE/../.venv/bin/zundamon-jarvis"
  [ -x "$ZJ" ] || ZJ="$(command -v zundamon-jarvis || true)"
  [ -n "$ZJ" ] || { echo "zundamon-jarvis not installed (uv sync)" >&2; exit 1; }
  if [ "$FLAVOR" = cpu ]; then "$ZJ" engine install --cpu; else "$ZJ" engine install; fi
fi

mkdir -p "$UNIT_DIR"
cp "$HERE/systemd/voicevox.service" "$UNIT_DIR/voicevox.service"
if [ "$FLAVOR" = cpu ]; then
  sed -i 's/ --use_gpu//' "$UNIT_DIR/voicevox.service"
fi
systemctl --user daemon-reload
systemctl --user enable --now voicevox.service
if [ "$MOVED" = 1 ]; then systemctl --user restart voicevox.service; fi  # was stopped for the move

for _ in $(seq 1 60); do
  if curl -fs 127.0.0.1:50021/version; then echo; exit 0; fi
  sleep 2
done
echo "VOICEVOX did not come up; see: journalctl --user -u voicevox" >&2
exit 1
