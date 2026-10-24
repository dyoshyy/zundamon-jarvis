#!/usr/bin/env bash
# Remove the ZundamonJarvis echo-cancel config, restart PipeWire, and restore the previous default sink.
set -euo pipefail
. "$(dirname "$0")/audio-lib.sh"

systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
rm -f "$UNIT_DIR/$UNIT"; rm -rf "$BIN_DIR"
prev="$(cat "$PREV_FILE" 2>/dev/null || true)"
changed=0
if [ -L "$CONF_DEST" ] || [ -e "$CONF_DEST" ]; then rm -f "$CONF_DEST"; changed=1; fi
# restart if we changed the config, or the AEC nodes are still loaded
pactl list short sinks 2>/dev/null | cut -f2 | grep -qx "$AEC_SINK" && changed=1
if [ "$changed" = 1 ]; then restart_audio; fi

if [ -n "$prev" ]; then
  for _ in $(seq 50); do
    pactl list short sinks | cut -f2 | grep -qx "$prev" && break
    sleep 0.2
  done
  if pactl list short sinks | cut -f2 | grep -qx "$prev"; then
    pactl set-default-sink "$prev"
    echo "default sink restored: $prev"
  else
    echo "previous sink $prev is not available; default left as $(pactl get-default-sink)" >&2
  fi
  rm -f "$PREV_FILE"
else
  echo "no recorded previous default sink; default is $(pactl get-default-sink)"
fi
