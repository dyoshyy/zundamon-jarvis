"""Input devices the settings window offers, and the pure logic around them.

Linux: ``pactl -f json list sources`` is the list (PipeWire via pipewire-pulse: the same names
``mic_device`` and PULSE_SOURCE take, see ``zundamon_jarvis.audio_in``). Monitors (what a sink plays)
are not microphones and are left out. ``None`` stands for "follow the OS default input".

macOS: the CoreAudio devices ``sounddevice.query_devices()`` reports; a device with input
channels is a microphone and its name is what ``mic_device`` takes. ``query`` stands in for
``sounddevice.query_devices`` in tests (never the real CoreAudio there). PortAudio reads the
device list once, when it is initialised, so these answers are the ones the streams use too.
"""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zundamon_jarvis import platform
from zundamon_jarvis.config import valid_mic_device

DEFAULT_WORD = "default"  # the control-socket / CLI spelling of "follow the OS default"
DEFAULT_LABEL = "System default"
PACTL_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class MicSource:
    name: str
    label: str


def parse_sources(text: str) -> list[MicSource]:
    """Input sources from ``pactl -f json list sources`` output; [] if it is not that."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out: list[MicSource] = []
    for src in data:
        if not isinstance(src, dict) or not isinstance(src.get("name"), str):
            continue
        props = src.get("properties")
        props = props if isinstance(props, dict) else {}
        if src.get("monitor_source") or props.get("device.class") == "monitor":
            continue
        desc = src.get("description")
        out.append(MicSource(src["name"], desc if isinstance(desc, str) and desc else src["name"]))
    return out


Query = Callable[..., Any]  # sounddevice.query_devices


def _query_devices(*args: Any, **kw: Any) -> Any:
    import sounddevice as sd  # PortAudio is loaded only when a Mac asks

    return sd.query_devices(*args, **kw)


def _ask(query: Query, *args: Any, **kw: Any) -> Any:
    """``query(...)``, or None when PortAudio cannot answer (no library, no device)."""
    try:
        return query(*args, **kw)
    except Exception:  # noqa: BLE001 -- sounddevice.PortAudioError has no narrower base
        return None


def _mac_inputs(query: Query) -> list[str | None] | None:
    """Input device names by PortAudio index (None for an output-only device), or None if
    the devices cannot be listed."""
    devices = _ask(query)
    if not isinstance(devices, (list, tuple)):  # sounddevice.DeviceList is a tuple
        return None
    return [d.get("name") if isinstance(d, dict) and (d.get("max_input_channels") or 0) > 0
            and isinstance(d.get("name"), str) else None
            for d in devices]


def _mac_default_name(query: Query, kind: str) -> str | None:
    info = _ask(query, kind=kind)
    name = info.get("name") if isinstance(info, dict) else None
    return name if isinstance(name, str) and name else None


def input_device_index(name: str, query: Query = _query_devices) -> int | None:
    """macOS: the PortAudio index of the input device called exactly ``name``, else None."""
    return next((i for i, n in enumerate(_mac_inputs(query) or []) if n == name), None)


def output_is_builtin_speaker(system: str, query: Query = _query_devices) -> bool:
    """macOS: the default output is the Mac's own speakers (which the built-in mic hears).
    Headphones, AirPods, Bluetooth or external devices are not. Always False on Linux."""
    name = _mac_default_name(query, "output") if system == "mac" else None
    if name is None:
        return False
    lower = name.lower()
    return "speakers" in lower and not any(
        word in lower for word in ("bluetooth", "airpods", "headphone", "external"))


def list_sources(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run, *,
                 system: str | None = None, query: Query = _query_devices) -> list[MicSource]:
    if (system or platform.system()) == "mac":
        unique = dict.fromkeys(n for n in _mac_inputs(query) or [] if n is not None)
        return [MicSource(n, n) for n in unique]
    try:
        r = run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True,
                timeout=PACTL_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_sources(r.stdout)


def installed_source_names(
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run, *,
    system: str | None = None, query: Query = _query_devices,
) -> set[str] | None:
    """Names from ``pactl list sources short`` (macOS: the input devices); None if that cannot
    be asked (no pactl)."""
    if (system or platform.system()) == "mac":
        names = _mac_inputs(query)
        return None if names is None else {n for n in names if n is not None}
    try:
        r = run(["pactl", "list", "sources", "short"], capture_output=True, text=True,
                timeout=PACTL_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return {cols[1] for line in r.stdout.splitlines() if len(cols := line.split("\t")) > 1}


def default_source_name(
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run, *,
    system: str | None = None, query: Query = _query_devices,
) -> str | None:
    """The PipeWire name of the OS default input (what "System default" resolves to);
    macOS: the default input device's name."""
    if (system or platform.system()) == "mac":
        return _mac_default_name(query, "input")
    try:
        r = run(["pactl", "get-default-source"], capture_output=True, text=True,
                timeout=PACTL_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None


def parse_mic(raw: str) -> str | int | None:
    """A ``set mic`` value: "default" -> None; a number -> PortAudio index; else a source name."""
    raw = raw.strip()
    if raw.lower() == DEFAULT_WORD:
        return None
    value: str | int | None = int(raw) if raw.isascii() and raw.isdigit() else raw
    mic = valid_mic_device(value)
    if mic is None:
        raise ValueError(f"mic must be 'default', a mic name or a device index: {raw!r}")
    return mic


def mic_value(device: str | int | None) -> str:
    """The inverse of ``parse_mic`` (what goes over the control socket)."""
    return DEFAULT_WORD if device is None else str(device)


def mic_choices(sources: list[MicSource], current: str | int | None) -> list[tuple[str | int | None, str]]:
    """(device, label) in menu order: the OS default first, then the sources; the current
    device is kept in the list even if it is not plugged in."""
    choices: list[tuple[str | int | None, str]] = [(None, DEFAULT_LABEL)]
    choices += [(s.name, s.label) for s in sources]
    if current is not None and all(current != d for d, _ in choices):
        choices.append((current, f"{current} (not found)" if isinstance(current, str)
                        else f"Device #{current}"))
    return choices


def mic_label(sources: list[MicSource], device: str | int | None,
              default_name: str | None = None) -> str:
    """The menu label; "System default" shows which source it resolves to right now."""
    label = next(label for d, label in mic_choices(sources, device) if d == device)
    if device is None and default_name:
        label += f" ({next((s.label for s in sources if s.name == default_name), default_name)})"
    return label


def cycle_mic(sources: list[MicSource], current: str | int | None, delta: int) -> str | int | None:
    devices = [d for d, _ in mic_choices(sources, current)]
    return devices[(devices.index(current) + delta) % len(devices)]
