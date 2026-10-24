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
