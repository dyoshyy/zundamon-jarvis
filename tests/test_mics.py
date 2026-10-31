"""Input device listing and the pure choice logic."""

import json
import subprocess

import pytest

from zundamon_jarvis import mics
from zundamon_jarvis.mics import MicSource

PACTL = json.dumps([
    {"name": "alsa_output.x.monitor", "description": "Monitor of HDMI", "monitor_source": "",
     "properties": {"device.class": "monitor"}},
    {"name": "alsa_input.cam", "description": "Webcam", "monitor_source": "",
     "properties": {"device.class": "sound"}},
    {"name": "bluez_input.AA", "description": "", "properties": {}},
    {"name": "bluez_output.AA.monitor", "description": "Monitor of Headset",
     "monitor_source": "bluez_output.AA", "properties": {}},
    {"description": "no name"},
    "junk",
])
SOURCES = [MicSource("alsa_input.cam", "Webcam"), MicSource("bluez_input.AA", "bluez_input.AA")]


def test_parse_sources_drops_monitors_and_junk():
    assert mics.parse_sources(PACTL) == SOURCES


@pytest.mark.parametrize("text", ["", "not json", "{}", "null"])
def test_parse_sources_survives_garbage(text):
    assert mics.parse_sources(text) == []


def test_list_sources_runs_pactl_and_survives_its_absence():
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, PACTL, "")

    assert mics.list_sources(run) == SOURCES and seen == [["pactl", "-f", "json", "list", "sources"]]

    def missing(cmd, **kw):
        raise FileNotFoundError

    def fails(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    assert mics.list_sources(missing) == [] and mics.list_sources(fails) == []


def test_parse_mic():
    assert mics.parse_mic("default") is None and mics.parse_mic(" Default ") is None
    assert mics.parse_mic("3") == 3
    assert mics.parse_mic("zundamon_jarvis_aec_source") == "zundamon_jarvis_aec_source"
    for bad in ("", "  "):
        with pytest.raises(ValueError):
            mics.parse_mic(bad)


def test_mic_value_round_trips():
    for d in (None, 3, "alsa_input.cam"):
        assert mics.parse_mic(mics.mic_value(d)) == d


def test_choices_put_the_os_default_first_and_keep_an_unplugged_current():
    assert [d for d, _ in mics.mic_choices(SOURCES, None)] == [None, "alsa_input.cam", "bluez_input.AA"]
    ch = mics.mic_choices(SOURCES, "gone")
    assert ch[-1] == ("gone", "gone (not found)")
    assert mics.mic_choices([], 2) == [(None, mics.DEFAULT_LABEL), (2, "Device #2")]


def test_mic_label_and_cycle():
    assert mics.mic_label(SOURCES, None) == "System default"
    assert mics.mic_label(SOURCES, "alsa_input.cam") == "Webcam"
    assert mics.cycle_mic(SOURCES, None, +1) == "alsa_input.cam"
    assert mics.cycle_mic(SOURCES, None, -1) == "bluez_input.AA"  # wraps
    assert mics.cycle_mic(SOURCES, "bluez_input.AA", +1) is None
    assert mics.cycle_mic([], None, +1) is None


def test_default_source_name_asks_pactl_and_survives_its_absence():
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "alsa_input.cam\n", "")

    assert mics.default_source_name(run) == "alsa_input.cam"
    assert seen == [["pactl", "get-default-source"]]

    def broken(cmd, **kw):
        raise FileNotFoundError("pactl")

    assert mics.default_source_name(broken) is None


def test_default_choice_shows_the_resolved_source_name():
    assert mics.mic_label(SOURCES, None, default_name="alsa_input.cam") == "System default (Webcam)"
    assert mics.mic_label(SOURCES, None, default_name="unlisted") == "System default (unlisted)"
    assert mics.mic_label(SOURCES, None) == "System default"
    assert mics.mic_label(SOURCES, "alsa_input.cam", default_name="alsa_input.cam") == "Webcam"


def test_installed_source_names_parses_pactl_short():
    out = "47\talsa_input.cam\tPipeWire\ts16le 1ch 16000Hz\tSUSPENDED\n48\tbluez_input.AA\tx\n\n"

    def run(cmd, **kw):
        assert cmd == ["pactl", "list", "sources", "short"]
        return subprocess.CompletedProcess(cmd, 0, out, "")

    assert mics.installed_source_names(run) == {"alsa_input.cam", "bluez_input.AA"}

    def broken(cmd, **kw):
        raise FileNotFoundError("pactl")

    assert mics.installed_source_names(broken) is None


# ---- macOS: CoreAudio devices through sounddevice (a fixture, never the real query) ----

MAC_DEVICES = [
    {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "USB Audio Device", "max_input_channels": 1, "max_output_channels": 2},
    {"name": "AirPods Pro", "max_input_channels": 1, "max_output_channels": 2},
    {"name": "AirPods Pro", "max_input_channels": 1, "max_output_channels": 2},  # duplicate
    {"name": "BlackHole 2ch", "max_input_channels": 0, "max_output_channels": 2},
]


def mac_query(default_in=0, default_out=1, devices=MAC_DEVICES):
    def query(device=None, kind=None):
        if kind == "input":
            return devices[default_in]
        if kind == "output":
            return devices[default_out]
        if device is not None:
            return devices[device]
        return list(devices)

    return query


def test_mac_lists_input_devices_from_sounddevice():
    got = mics.list_sources(system="mac", query=mac_query())
    assert got == [MicSource("MacBook Pro Microphone", "MacBook Pro Microphone"),
                   MicSource("USB Audio Device", "USB Audio Device"),
                   MicSource("AirPods Pro", "AirPods Pro")]  # outputs-only left out, no duplicates


def test_mac_listing_never_runs_pactl_and_survives_a_broken_query():
    def run(cmd, **kw):
        raise AssertionError("pactl on a Mac")

    def broken(*a, **kw):
        raise OSError("PortAudio not initialized")

    assert mics.list_sources(run, system="mac", query=broken) == []
    assert mics.installed_source_names(run, system="mac", query=broken) is None
    assert mics.default_source_name(run, system="mac", query=broken) is None


def test_mac_installed_names_and_default_input():
    assert mics.installed_source_names(system="mac", query=mac_query()) == {
        "MacBook Pro Microphone", "USB Audio Device", "AirPods Pro"}
    assert mics.default_source_name(system="mac", query=mac_query(default_in=2)) == "USB Audio Device"


def test_mac_input_device_index_is_an_exact_name_match():
    q = mac_query()
    assert mics.input_device_index("USB Audio Device", query=q) == 2
    assert mics.input_device_index("AirPods Pro", query=q) == 3  # the first one
    assert mics.input_device_index("USB Audio", query=q) is None  # no substring matching
    assert mics.input_device_index("MacBook Pro Speakers", query=q) is None  # an output


@pytest.mark.parametrize("name,builtin", [
    ("MacBook Pro Speakers", True),
    ("MacBook Air Speakers", True),
    ("Mac mini Speakers", True),
    ("AirPods Pro", False),
    ("External Headphones", False),
    ("USB Audio Device", False),
    ("Bluetooth Speakers", False),
    ("JBL Flip 5 Speakers (Bluetooth)", False),
    ("External Speakers", False),
])
def test_output_is_builtin_speaker_on_mac(name, builtin):
    devices = [{"name": name, "max_input_channels": 0, "max_output_channels": 2}]
    assert mics.output_is_builtin_speaker("mac", query=mac_query(0, 0, devices)) is builtin


def test_output_is_builtin_speaker_is_false_on_linux_and_on_a_broken_query():
    def boom(*a, **kw):
        raise AssertionError("no CoreAudio query on Linux")

    assert mics.output_is_builtin_speaker("linux", query=boom) is False

    def broken(*a, **kw):
        raise OSError("no device")

    assert mics.output_is_builtin_speaker("mac", query=broken) is False


# ---- Windows: PortAudio lists each device once per host API; only MME is offered ----

HYPERX_MIC = "Headset Microphone (HyperX Cloud II Wireless)"
HYPERX_MIC_MME = HYPERX_MIC[:31]  # MME cuts names to 31 characters
WIN_HOSTAPIS = ["MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS"]
WIN_DEVICES = [  # (name, host API, inputs, outputs), as sounddevice lists them on a desktop PC
    ("Microsoft Sound Mapper - Input", 0, 2, 0),
    ("Microphone (Realtek(R) Audio)", 0, 2, 0),
    (HYPERX_MIC_MME, 0, 1, 0),
    ("Microsoft Sound Mapper - Output", 0, 0, 2),
    ("Speakers (Realtek(R) Audio)", 0, 0, 2),
    ("Primary Sound Capture Driver", 1, 2, 0),
    ("Microphone (Realtek(R) Audio)", 1, 2, 0),
    (HYPERX_MIC, 1, 1, 0),
    ("Primary Sound Driver", 1, 0, 2),
    ("Speakers (Realtek(R) Audio)", 1, 0, 2),
    ("Headphones (HyperX Cloud II Wireless)", 1, 0, 2),
    ("Microphone (Realtek(R) Audio)", 2, 2, 0),  # 11
    (HYPERX_MIC, 2, 1, 0),  # 12
    ("Speakers (Realtek(R) Audio)", 2, 0, 2),  # 13
    ("Headphones (HyperX Cloud II Wireless)", 2, 0, 2),  # 14
    ("Microphone (Realtek HD Audio Mic input)", 3, 2, 0),
]


def win(default_in=11, default_out=13, devices=WIN_DEVICES, apis=WIN_HOSTAPIS):
    devs = [{"name": n, "hostapi": h, "max_input_channels": i, "max_output_channels": o}
            for n, h, i, o in devices]
    defaults = {"MME": (0, 3), "Windows DirectSound": (5, 8),
                "Windows WASAPI": (default_in, default_out), "Windows WDM-KS": (15, -1)}

    def query(device=None, kind=None):
        assert kind is None, "Windows never asks sounddevice's default (MME's Sound Mapper)"
        return devs if device is None else devs[device]

    def hostapis(index=None):
        out = [{"name": a, "devices": [i for i, d in enumerate(devs) if d["hostapi"] == k],
                "default_input_device": defaults[a][0], "default_output_device": defaults[a][1]}
               for k, a in enumerate(apis)]
        return out if index is None else out[index]

    return {"query": query, "hostapis": hostapis}


def no_pactl(cmd, **kw):
    raise AssertionError("pactl on Windows")


def test_windows_lists_each_mme_input_once_with_its_full_name_as_label():
    assert mics.list_sources(no_pactl, system="windows", **win()) == [
        MicSource("Microphone (Realtek(R) Audio)", "Microphone (Realtek(R) Audio)"),
        MicSource(HYPERX_MIC_MME, HYPERX_MIC),  # the cut MME name, labelled in full
    ]  # no Sound Mapper, no DirectSound / WASAPI / WDM-KS duplicates, no outputs


def test_windows_installed_names_and_index_are_the_mme_ones():
    w = win()
    assert mics.installed_source_names(no_pactl, system="windows", **w) == {
        "Microphone (Realtek(R) Audio)", HYPERX_MIC_MME}
    assert mics.input_device_index("Microphone (Realtek(R) Audio)", system="windows", **w) == 1
    assert mics.input_device_index(HYPERX_MIC_MME, system="windows", **w) == 2
    assert mics.input_device_index(HYPERX_MIC, system="windows", **w) is None  # a DirectSound name
    assert mics.input_device_index("Microsoft Sound Mapper - Input", system="windows", **w) is None
    assert mics.input_device_index("Microphone (Realtek(R) Audio)", query=w["query"]) == 1  # mac: any


def test_windows_default_input_is_read_from_wasapi_and_named_as_in_mme():
    assert mics.default_source_name(no_pactl, system="windows", **win(default_in=12)) == HYPERX_MIC_MME
    assert mics.default_source_name(no_pactl, system="windows", **win(default_in=11)) == \
        "Microphone (Realtek(R) Audio)"
    sources = mics.list_sources(system="windows", **win())
    assert mics.mic_label(sources, None, HYPERX_MIC_MME) == f"System default ({HYPERX_MIC})"


def test_windows_default_input_without_wasapi_uses_directsound_and_skips_its_alias():
    no_wasapi = [d for d in WIN_DEVICES if d[1] < 2]
    w = win(devices=no_wasapi, apis=WIN_HOSTAPIS[:2])
    # DirectSound's default is "Primary Sound Capture Driver", an alias: no answer, not a wrong one
    assert mics.default_source_name(system="windows", **w) is None
    assert [s.name for s in mics.list_sources(system="windows", **w)] == [
        "Microphone (Realtek(R) Audio)", HYPERX_MIC_MME]


def test_windows_survives_a_broken_portaudio():
    def broken(*a, **kw):
        raise OSError("PortAudio not initialized")

    for kw in ({"query": broken, "hostapis": broken}, {"query": win()["query"], "hostapis": broken}):
        assert mics.list_sources(no_pactl, system="windows", **kw) == []
        assert mics.installed_source_names(no_pactl, system="windows", **kw) is None
        assert mics.default_source_name(no_pactl, system="windows", **kw) is None
        assert mics.output_is_headphones("windows", **kw) is False


def test_mme_name_matching_needs_one_cut_name():
    mme = ["Microphone (Realtek(R) Audio)", HYPERX_MIC_MME, "Mic"]
    assert mics.mme_name_for("Microphone (Realtek(R) Audio)", mme) == "Microphone (Realtek(R) Audio)"
    assert mics.mme_name_for(HYPERX_MIC, mme) == HYPERX_MIC_MME
    assert mics.mme_name_for("Microphone Array (Intel)", mme) is None  # "Mic" is not a cut name
    assert mics.mme_name_for(HYPERX_MIC, [HYPERX_MIC_MME, HYPERX_MIC_MME[:-1] + "!"]) == HYPERX_MIC_MME
    twins = "Microphone (2- USB PnP Sound Device)"
    assert mics.mme_name_for(twins, [twins[:31]]) == twins[:31]
    assert mics.full_label_for("Mic", ["Mic (USB)"]) == "Mic"  # not cut: kept as is
    assert mics.full_label_for(HYPERX_MIC_MME, [HYPERX_MIC, HYPERX_MIC_MME + " 2"]) == HYPERX_MIC_MME


@pytest.mark.parametrize("name,ears", [
    ("Headphones (HyperX Cloud II Wireless)", True),
    ("Headset Earphone (Logitech G435)", True),
    ("Speakers (USB Headset)", True),  # a USB headset's output is often called "Speakers"
    ("Headset (WH-1000XM4 Hands-Free AG Audio)", True),
    ("ヘッドホン (Realtek(R) Audio)", True),
    ("ヘッドセット (Galaxy Buds2 Pro)", True),
    ("イヤホン (Realtek(R) Audio)", True),
    ("AirPods Pro", True),
    ("Speakers (Realtek(R) Audio)", False),
    ("スピーカー (Realtek(R) Audio)", False),
    ("Realtek HD Audio 2nd output (Realtek(R) Audio)", False),  # a front jack: cannot tell
    ("LG ULTRAGEAR (NVIDIA High Definition Audio)", False),
])
def test_output_is_headphones_on_windows(name, ears):
    devices = [d for d in WIN_DEVICES if d[1] != 2] + [(name, 2, 0, 2)]
    w = win(default_in=-1, default_out=len(devices) - 1, devices=devices)
    assert mics.output_is_headphones("windows", **w) is ears


def test_output_is_headphones_only_asks_on_windows():
    def boom(*a, **kw):
        raise AssertionError("no query")

    assert mics.output_is_headphones("linux", query=boom, hostapis=boom) is False
    assert mics.output_is_headphones("mac", query=boom, hostapis=boom) is False
    assert mics.output_is_builtin_speaker("windows", query=boom) is False
