#!/usr/bin/env bash
# Choose where ZundamonJarvis audio comes out, persistently (applied now and re-applied by zundamon-jarvis-aec-output.service).
# Usage: deploy/aec-output.sh auto|bluez|hdmi|<sink name>   (no argument: show choice + list sinks)
#   auto = Bluetooth headphones when connected, otherwise HDMI.
set -euo pipefail
. "$(dirname "$0")/audio-lib.sh"
if [ -z "${1:-}" ]; then
  echo "choice: $(cat "$CHOICE_FILE" 2>/dev/null || echo auto)  -> $(resolve_target)"; sink_names; exit 0
fi
mkdir -p "$(dirname "$CHOICE_FILE")"
echo "$1" > "$CHOICE_FILE"
apply_choice
echo "echo-cancel output: $1 -> $(resolve_target)"
