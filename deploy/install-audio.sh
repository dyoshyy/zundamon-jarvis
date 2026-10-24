#!/usr/bin/env bash
# Install the ZundamonJarvis echo-cancel PipeWire config and make zundamon_jarvis_aec_sink the default sink.
# Idempotent. Restarts pipewire/pipewire-pulse/wireplumber only when the config link changes
# or the AEC nodes are missing (audio drops for a second or two).
# Back out with deploy/uninstall-audio.sh.
set -euo pipefail
. "$(dirname "$0")/audio-lib.sh"

[ -f "$CONF_SRC" ] || { echo "missing $CONF_SRC" >&2; exit 1; }
mkdir -p "$CONF_DIR" "$STATE_DIR"

need_restart=0
[ -L "$CONF_DEST" ] && rm -f "$CONF_DEST"  # replace an old symlink with a real copy
if ! cmp -s "$CONF_SRC" "$CONF_DEST"; then
  cp "$CONF_SRC" "$CONF_DEST"
  need_restart=1
fi
# scripts + unit that keep the AEC output on the right device across logins / Bluetooth reconnects
mkdir -p "$BIN_DIR" "$UNIT_DIR"
cp "$(dirname "$0")/audio-lib.sh" "$(dirname "$0")/aec-follow.sh" "$(dirname "$0")/aec-output.sh" "$BIN_DIR/"
cp "$(dirname "$0")/systemd/$UNIT" "$UNIT_DIR/$UNIT"
chmod +x "$BIN_DIR"/*.sh
pactl list short sinks 2>/dev/null | cut -f2 | grep -qx "$AEC_SINK" || need_restart=1

# Record the sink to restore later, unless that is already us (re-run) or already recorded.
current="$(pactl get-default-sink 2>/dev/null || true)"
if [ -n "$current" ] && [ "$current" != "$AEC_SINK" ]; then
  echo "$current" > "$PREV_FILE"
fi
prev="$(cat "$PREV_FILE" 2>/dev/null || true)"

if [ "$need_restart" = 1 ]; then restart_audio; fi
[ "${ZUNDAMON_JARVIS_AUDIO_NO_RESTART:-0}" = 1 ] && exit 0
wait_for_sink "$AEC_SINK" || { echo "$AEC_SINK did not appear" >&2; exit 1; }

# Output goes to the device that was the default before we took over.
if [ -n "$prev" ]; then wait_for_sink "$prev" 75 || true; fi  # Bluetooth sinks reappear slowly
if [ -z "$prev" ] || ! pactl list short sinks | cut -f2 | grep -qx "$prev"; then
  prev="$(pactl list short sinks | cut -f2 | grep -v "^$AEC_SINK" | head -1 || true)"
  echo "previous sink unavailable; using $prev" >&2
fi
sleep 1
pactl set-default-sink "$AEC_SINK"
if [ ! -f "$CHOICE_FILE" ]; then  # first install: keep whatever the user was listening on
  mkdir -p "$(dirname "$CHOICE_FILE")"; echo auto > "$CHOICE_FILE"
fi
systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null 2>&1
systemctl --user restart "$UNIT"
sleep 3  # let the follower apply the choice
echo "default sink: $AEC_SINK; output: $(resolve_target) (choice: $(cat "$CHOICE_FILE")); previous default $prev saved in $PREV_FILE"
pactl list short sinks; pactl list short sources
