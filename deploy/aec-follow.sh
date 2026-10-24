#!/usr/bin/env bash
# Keeps zundamon_jarvis_aec_playback pointed at the right real device (see resolve_target in audio-lib.sh).
# Runs as the systemd user unit zundamon-jarvis-aec-output.service: applies at start and whenever a
# sink appears or disappears (Bluetooth connect/disconnect, HDMI, PipeWire restart).
set -uo pipefail
. "$(dirname "$0")/audio-lib.sh"
apply_all() { local i; for i in 1 2 3 4 5 6 7 8 9 10; do apply_choice && return 0; sleep 1; done; }
apply_all
pactl subscribe | while read -r _ _ _ what _; do
  case "$what" in sink|server) sleep 1; apply_choice || true ;; esac
done
exit 1  # pactl subscribe ended (pipewire-pulse restarted): let systemd restart us
