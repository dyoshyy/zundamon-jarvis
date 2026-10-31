"""Input devices the settings window offers, and the pure logic around them.

Linux: ``pactl -f json list sources`` is the list (PipeWire via pipewire-pulse: the same names
``mic_device`` and PULSE_SOURCE take, see ``zundamon_jarvis.audio_in``). Monitors (what a sink plays)
are not microphones and are left out. ``None`` stands for "follow the OS default input".

macOS: the CoreAudio devices ``sounddevice.query_devices()`` reports; a device with input
channels is a microphone and its name is what ``mic_device`` takes. ``query`` stands in for
``sounddevice.query_devices`` in tests (never the real CoreAudio there). PortAudio reads the
device list once, when it is initialised, so these answers are the ones the streams use too.

Windows: there is no pactl either, so it is ``sounddevice`` again, but PortAudio lists every
device once per host API (MME, DirectSound, WASAPI, WDM-KS). Only the **MME** devices are
offered, which makes names unique and is the host API that opens reliably at what the
pipeline needs (16 kHz mono float32): MME goes through the Windows mixer, which resamples to
any rate, and it is PortAudio's default host API there, so ``None`` (sounddevice's default
input, the "Microsoft Sound Mapper") follows the Windows default recording device. WASAPI
shared mode refuses a rate other than the device's mix format (44.1/48 kHz) unless asked to
convert, and WDM-KS opens the device exclusively. The cost: MME names are cut to 31
characters ("Microphone (Realtek(R) Audio)" fits, longer ones do not), so a WASAPI or
DirectSound name that starts with a cut MME name is used as its label; the name stays the
MME one (what ``mic_device`` takes and ``input_device_index`` finds). The Sound Mapper itself
is not listed: it is "System default". ``hostapis`` stands in for
``sounddevice.query_hostapis`` in tests.

The default output (``output_is_headphones``) is read from WASAPI (else DirectSound): MME's
default is the Sound Mapper, which does not say what it plays through.
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
    import sounddevice as sd  # PortAudio is loaded only when a Mac or Windows asks

    return sd.query_devices(*args, **kw)


HostApis = Callable[..., Any]  # sounddevice.query_hostapis


def _query_hostapis(*args: Any, **kw: Any) -> Any:
    import sounddevice as sd

    return sd.query_hostapis(*args, **kw)


WINDOWS_HOSTAPI = "MME"
WINDOWS_FULL_NAME_HOSTAPIS = ("Windows WASAPI", "Windows DirectSound")  # in order of preference
MME_NAME_MAX = 31  # WAVEINCAPS.szPname is 32 characters with the terminating NUL
_WINDOWS_ALIASES = frozenset({  # not devices: "whatever Windows' default is"
    "Microsoft Sound Mapper - Input", "Microsoft Sound Mapper - Output",
    "Primary Sound Capture Driver", "Primary Sound Driver",
})
# Output device names that mean the sound goes into ears, not into the room (Windows names an
# output after its jack or kind: "Speakers (…)", "Headphones (…)", "ヘッドホン (…)").
HEADPHONE_WORDS = ("headphone", "headset", "earphone", "earbud", "buds", "airpods",
                   "hands-free", "handsfree", "ヘッドホン", "ヘッドフォン", "ヘッドセット",
                   "イヤホン", "イヤフォン")


def _ask(query: Query, *args: Any, **kw: Any) -> Any:
    """``query(...)``, or None when PortAudio cannot answer (no library, no device)."""
    try:
        return query(*args, **kw)
    except Exception:  # noqa: BLE001 -- sounddevice.PortAudioError has no narrower base
        return None


def _hostapi_index(hostapis: HostApis, name: str) -> int | None:
    apis = _ask(hostapis)
    if not isinstance(apis, (list, tuple)):
        return None
    return next((i for i, a in enumerate(apis) if isinstance(a, dict) and a.get("name") == name),
                None)


def _input_name(d: Any, hostapi: int | None = None) -> str | None:
    if not (isinstance(d, dict) and (d.get("max_input_channels") or 0) > 0
            and isinstance(d.get("name"), str)):
        return None
    if hostapi is not None and (d.get("hostapi") != hostapi or d["name"] in _WINDOWS_ALIASES):
        return None
    return d["name"]


def _inputs(system: str, query: Query, hostapis: HostApis) -> list[str | None] | None:
    """Input device names by PortAudio index (None for an output-only device, and on Windows
    for one of another host API), or None if the devices cannot be listed."""
    devices = _ask(query)
    if not isinstance(devices, (list, tuple)):  # sounddevice.DeviceList is a tuple
        return None
    if system != "windows":
        return [_input_name(d) for d in devices]
    api = _hostapi_index(hostapis, WINDOWS_HOSTAPI)
    if api is None:
        return None
    return [_input_name(d, api) for d in devices]


def _windows_default_name(query: Query, hostapis: HostApis, kind: str) -> str | None:
    """The full name of Windows' default input or output (``kind``), from WASAPI or else
    DirectSound."""
    apis = _ask(hostapis)
    if not isinstance(apis, (list, tuple)):
        return None
    for want in WINDOWS_FULL_NAME_HOSTAPIS:
        api = next((a for a in apis if isinstance(a, dict) and a.get("name") == want), None)
        index = api.get(f"default_{kind}_device") if api else None
        if isinstance(index, int) and index >= 0:
            info = _ask(query, index)
            name = info.get("name") if isinstance(info, dict) else None
            if isinstance(name, str) and name and name not in _WINDOWS_ALIASES:
                return name
    return None


def _windows_full_inputs(query: Query, hostapis: HostApis) -> list[str]:
    devices = _ask(query)
    if not isinstance(devices, (list, tuple)):
        return []
    apis = {_hostapi_index(hostapis, n) for n in WINDOWS_FULL_NAME_HOSTAPIS} - {None}
    return [d["name"] for d in devices
            if _input_name(d) is not None and d.get("hostapi") in apis]


def mme_name_for(full: str, mme_names: list[str]) -> str | None:
    """The MME name of the device called ``full`` elsewhere: the same name, or the one cut
    MME name (31 characters) that ``full`` starts with; None if there is not exactly one."""
    if full in mme_names:
        return full
    cut = {n for n in mme_names if len(n) >= MME_NAME_MAX and full.startswith(n)}
    return cut.pop() if len(cut) == 1 else None


def full_label_for(mme: str, full_names: list[str]) -> str:
    """The label of an MME device: the one longer name it is the cut-off start of, else itself."""
    if len(mme) < MME_NAME_MAX:
        return mme
    longer = {n for n in full_names if n != mme and n.startswith(mme)}
    return longer.pop() if len(longer) == 1 else mme


def _mac_default_name(query: Query, kind: str) -> str | None:
    info = _ask(query, kind=kind)
    name = info.get("name") if isinstance(info, dict) else None
    return name if isinstance(name, str) and name else None


def input_device_index(name: str, query: Query = _query_devices, *, system: str = "mac",
                       hostapis: HostApis = _query_hostapis) -> int | None:
    """macOS / Windows: the PortAudio index of the input device called exactly ``name`` (on
    Windows an MME one), else None."""
    names = _inputs(system, query, hostapis) or []
    return next((i for i, n in enumerate(names) if n == name), None)


def output_is_headphones(system: str, query: Query = _query_devices,
                         hostapis: HostApis = _query_hostapis) -> bool:
    """Windows: the default output's name says headphones, a headset or earbuds (the mic will
    not hear what is played). False when it does not, or cannot be asked: speakers are the
    safe guess. Always False elsewhere."""
    if system != "windows":
        return False
    name = _windows_default_name(query, hostapis, "output")
    return name is not None and any(word in name.lower() for word in HEADPHONE_WORDS)


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
                 system: str | None = None, query: Query = _query_devices,
                 hostapis: HostApis = _query_hostapis) -> list[MicSource]:
    sys_ = system or platform.system()
    if sys_ in ("mac", "windows"):
        unique = dict.fromkeys(n for n in _inputs(sys_, query, hostapis) or [] if n is not None)
        if sys_ == "mac":
            return [MicSource(n, n) for n in unique]
        full = _windows_full_inputs(query, hostapis)
        return [MicSource(n, full_label_for(n, full)) for n in unique]
    try:
        r = run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True,
                timeout=PACTL_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_sources(r.stdout)


def installed_source_names(
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run, *,
    system: str | None = None, query: Query = _query_devices, hostapis: HostApis = _query_hostapis,
) -> set[str] | None:
    """Names from ``pactl list sources short`` (macOS: the input devices, Windows: the MME
    ones); None if that cannot be asked (no pactl)."""
    sys_ = system or platform.system()
    if sys_ in ("mac", "windows"):
        names = _inputs(sys_, query, hostapis)
        return None if names is None else {n for n in names if n is not None}
    try:
        r = run(["pactl", "list", "sources", "short"], capture_output=True, text=True,
                timeout=PACTL_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return {cols[1] for line in r.stdout.splitlines() if len(cols := line.split("\t")) > 1}


def default_source_name(
    run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run, *,
    system: str | None = None, query: Query = _query_devices, hostapis: HostApis = _query_hostapis,
) -> str | None:
    """The PipeWire name of the OS default input (what "System default" resolves to);
    macOS: the default input device's name; Windows: its MME name (its full name when no MME
    device matches it)."""
    sys_ = system or platform.system()
    if sys_ == "mac":
        return _mac_default_name(query, "input")
    if sys_ == "windows":
        full = _windows_default_name(query, hostapis, "input")
        if full is None:
            return None
        mme = [n for n in _inputs(sys_, query, hostapis) or [] if n is not None]
        return mme_name_for(full, mme) or full
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
