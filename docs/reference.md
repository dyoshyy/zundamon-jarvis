# Reference

## Installer options

| variable | effect |
|---|---|
| `ZJ_VERSION=v0.1.0` | install that release instead of the latest |
| `ZJ_DRY_RUN=1` | print what would run, change nothing |
| `ZJ_LANG=ja\|en` | installer language (default: the OS's) |
| `ZJ_CLI=1` (Mac) | do everything in the terminal (`engine install`, `models download`, `setup --cli`) |

On Windows set them with `$env:ZJ_VERSION = 'v0.1.0'` before the one-liner.

## Setup

`zundamon-jarvis setup` serves the setup page from 127.0.0.1 and shows it in an app window (pywebview: WKWebView
on Mac, WebView2 on Windows). On Linux, or when no window can be made, it opens in the browser instead and
the terminal prints the URL. `setup --cli` asks the same questions in the terminal. Every answer is saved as soon as it is given.

## `config.toml`

| key | meaning |
|---|---|
| `workdir` | where Claude Code runs (its `CLAUDE.md`, memory and hooks apply). Default: home |
| `knowledge_dir` | a notes folder given to Claude as an extra directory. Default: none |
| `model` | `sonnet` (default), `opus` or `haiku` |
| `speed` | voice speed, 0.8–2.0 (default 1.2) |
| `speaker_id` | VOICEVOX style id; audition with `zundamon-jarvis voices` |
| `mic_device` | input device name or PortAudio index. Default: the OS default |
| `duplex` | `auto` (default), `full` (barge-in) or `half` (mic ignored while she speaks). `auto` = half on Mac built-in speakers and on Windows without headphones |
| `language` | display language `ja` / `en`. Default: the OS's |

Comments and unknown keys are kept when the tool edits the file. `zundamon-jarvisctl get` / `set speed|speaker|model|mic|language <value>` changes them live.

## Where things are

| | Linux | macOS | Windows |
|---|---|---|---|
| config | `~/.config/zundamon-jarvis/` | `~/Library/Application Support/zundamon-jarvis/` | `%APPDATA%\zundamon-jarvis\` |
| engine, models | `~/.local/share/zundamon-jarvis/` | `~/Library/Application Support/zundamon-jarvis/` | `%LOCALAPPDATA%\zundamon-jarvis\` |
| logs | `~/.local/state/zundamon-jarvis/` | `~/Library/Logs/zundamon-jarvis/` | `%LOCALAPPDATA%\zundamon-jarvis\logs\` |
| service | systemd user units | LaunchAgents | `ZundamonJarvis` in `HKCU\…\CurrentVersion\Run` |

## Windows service

Windows has no per-user service manager, so `zundamon-jarvis supervise` does the job. It is started at login
by the Run key, through the windowless `zundamon-jarvisw.exe`. It runs these without console windows:

- the VOICEVOX engine
- the daemon
- the tray icon

It restarts them the way the Linux systemd unit does: it gives up after 5 crashes in 5 minutes, and a quit tray stays quit.

Commands:

- `zundamon-jarvis service status | stop | start`
- `zundamon-jarvis uninstall` removes the login entry.

If it doesn't start at login, check that **ZundamonJarvis** is on in Settings > Apps > Startup.

## Updating

`zundamon-jarvis update` (`--check` only looks: exit 1 = an update is out; `--yes` skips the question).

- **Mac and Windows:**
  - The menu checks at most every 12 h and offers the update.
  - The update runs in the background: it stops the service, reinstalls with `uv tool install`, and starts it again.
  - It logs to `update.log` in the logs folder.
- **A git checkout (the Linux install):** `git pull --ff-only`, `uv sync`, then a restart. It only runs when the tree is clean.

## Linux extras

- **Without the setup page:**
  1. `uv run zundamon-jarvis engine install` (add `--cpu` for the CPU build)
  2. `uv run zundamon-jarvis models download`
  3. `uv run zundamon-jarvis setup --cli`
- **Units by hand:**
  1. Copy `deploy/systemd/*.service` to `~/.config/systemd/user/`.
  2. `systemctl --user daemon-reload && systemctl --user enable --now voicevox zundamon-jarvis`.
  - Logs: `journalctl --user -u zundamon-jarvis`.
- **Echo cancellation** (PipeWire WebRTC AEC, lets you use speakers):
  1. Run `deploy/install-audio.sh`.
  2. Set `mic_device = "zundamon_jarvis_aec_source"`.
  - To pin a mic, edit `deploy/pipewire/zundamon-jarvis-echo-cancel.conf` first.
- **Omarchy / Hyprland:**
  - Bind a key to `zundamon-jarvisctl toggle`.
  - Add a bar module running `zundamon-jarvisctl bar`. Click opens `zundamon-jarvisctl window`; right-click opens `window settings`.
  - Float the classes `zundamon-jarvis-watch` and `zundamon-jarvis-settings`.
  - `zundamon-jarvisctl character` puts Zundamon on screen. Drag to move, click for the conversation, right-click to toggle the speech bubble.

## Troubleshooting

- **Mac, mic unavailable:** System Settings > Privacy & Security > Microphone. Turn on **python3.12** (the daemon) and your terminal.
- **Windows, mic unavailable:** Settings > Privacy & security > Microphone. Turn on **Let desktop apps access your microphone**.
- **Windows, nothing after login:** run `zundamon-jarvis service status`. Then check `supervisor.log`, `daemon.log`, `engine.log` and `tray.log` in the logs folder.
- **Not logged in:** run `claude` and log in.
- **She hears herself:** use headphones. On Linux you can also set up echo cancellation.
- **Anything else:** attach the output of `zundamon-jarvis doctor` to an issue. The report leaves out speech, names, paths and e-mail addresses.
