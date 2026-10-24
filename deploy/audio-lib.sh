# shared helpers for install-audio.sh / uninstall-audio.sh / aec-output.sh (sourced)
CONF_NAME=zundamon-jarvis-echo-cancel.conf
CONF_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pipewire/$CONF_NAME"
CONF_DIR="$HOME/.config/pipewire/pipewire.conf.d"
CONF_DEST="$CONF_DIR/$CONF_NAME"   # a copy, not a symlink: survives branch switches / moved repo
BIN_DIR="$HOME/.local/share/zundamon-jarvis/audio"   # copies of the scripts the systemd unit runs
UNIT_DIR="$HOME/.config/systemd/user"
UNIT=zundamon-jarvis-aec-output.service
CHOICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/zundamon-jarvis/aec-output"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/zundamon-jarvis"
PREV_FILE="$STATE_DIR/previous-default-sink"
AEC_SINK=zundamon_jarvis_aec_sink
AEC_PLAYBACK=zundamon_jarvis_aec_playback

restart_audio() {
  if [ "${ZUNDAMON_JARVIS_AUDIO_NO_RESTART:-0}" = 1 ]; then echo "(skipping PipeWire restart)"; return; fi
  systemctl --user restart pipewire pipewire-pulse wireplumber
}

wait_for_sink() {  # wait_for_sink NAME [tries]
  local i
  for i in $(seq "${2:-50}"); do
    pactl list short sinks 2>/dev/null | cut -f2 | grep -qx "$1" && return 0
    sleep 0.2
  done
  return 1
}

node_id() {  # node_id NODE_NAME -> PipeWire object id
  pw-dump | python3 -c '
import json, sys
for o in json.load(sys.stdin):
    if o.get("info", {}).get("props", {}).get("node.name") == sys.argv[1]:
        print(o["id"]); break' "$1"
}

set_aec_output() {  # set_aec_output REAL_SINK_NAME : point the AEC playback side at a real device
  local id; id="$(node_id "$AEC_PLAYBACK")"
  [ -n "$id" ] || { echo "node $AEC_PLAYBACK not found" >&2; return 1; }
  pw-metadata "$id" target.object "$1" >/dev/null
}

# --- output choice -------------------------------------------------------
# CHOICE_FILE holds: auto (default) | bluez | hdmi | <exact sink name>.
# auto: the Bluetooth headphones when connected, otherwise the HDMI output.
sink_names() { pactl list short sinks | cut -f2; }

resolve_target() {  # resolve_target [CHOICE] -> real sink name to feed (may be empty)
  local c="${1:-$(cat "$CHOICE_FILE" 2>/dev/null || echo auto)}" names t
  names="$(sink_names || true)"
  case "$c" in
    auto|bluez|hdmi) ;;
    *) if grep -qx "$c" <<<"$names"; then echo "$c"; return; fi; c=auto ;;
  esac
  if [ "$c" != hdmi ]; then t="$(grep '^bluez_output' <<<"$names" | head -1 || true)"; fi
  if [ -z "${t:-}" ]; then t="$(grep 'hdmi' <<<"$names" | head -1 || true)"; fi
  if [ -z "${t:-}" ]; then t="$(grep -v "^$AEC_SINK" <<<"$names" | head -1 || true)"; fi
  echo "$t"
}

apply_choice() {  # point the AEC playback at the resolved sink; no-op if already there
  local t id cur
  t="$(resolve_target)"; [ -n "$t" ] || return 0
  id="$(node_id "$AEC_PLAYBACK")"; [ -n "$id" ] || return 0
  cur="$(pw-metadata "$id" target.object 2>/dev/null | sed -n "s/.*value:'\([^']*\)'.*/\1/p" | tail -1)"
  [ "$cur" = "$t" ] || { pw-metadata "$id" target.object "$t" >/dev/null; echo "aec output -> $t"; }
}
