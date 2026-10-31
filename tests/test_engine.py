import hashlib
from pathlib import Path

import pytest

from zundamon_jarvis import engine


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _fetch_factory(blobs: dict[str, bytes]):
    def fetch(url, dest):
        Path(dest).write_bytes(blobs[url])

    return fetch


def _extract_ok(parts, into, tar):
    root = Path(into) / "voicevox_engine"
    root.mkdir(parents=True)
    run = root / "run"
    run.write_text("#!/bin/sh\n", encoding="utf-8")
    run.chmod(0o755)


def _patch(monkeypatch, blobs, system="mac"):
    monkeypatch.setitem(
        engine.ENGINE, system, tuple((u, _sha(b)) for u, b in blobs.items())
    )


def test_engine_rejects_bad_checksum(tmp_path, monkeypatch):
    blobs = {"http://x/a.001": b"hello"}
    monkeypatch.setitem(engine.ENGINE, "mac", (("http://x/a.001", "0" * 64),))
    dest = tmp_path / "data" / "voicevox_engine"
    with pytest.raises(engine.ChecksumError):
        engine.install_engine("mac", dest, fetch=_fetch_factory(blobs), extract=_extract_ok, check=lambda r: None)
    assert not dest.exists()
    assert not [p for p in (tmp_path / "data").iterdir()]  # download dir cleaned up


def test_engine_skips_when_installed(tmp_path, monkeypatch):
    dest = tmp_path / "voicevox_engine"
    dest.mkdir()
    (dest / "run").write_text("x", encoding="utf-8")
    (dest / "run").chmod(0o755)
    (dest / engine.MARKER).write_text(engine.ENGINE["version"], encoding="utf-8")
    called = []
    engine.install_engine("mac", dest, fetch=lambda *a: called.append(a), extract=_extract_ok, check=lambda r: None)
    assert called == []


def test_engine_installs_verified_parts(tmp_path, monkeypatch):
    blobs = {"http://x/a.001": b"aa", "http://x/a.002": b"bb"}
    _patch(monkeypatch, blobs)
    seen = {}

    def extract(parts, into, tar):
        seen["names"] = [p.name for p in parts]
        seen["tar"] = tar
        _extract_ok(parts, into, tar)

    dest = tmp_path / "d" / "voicevox_engine"
    engine.install_engine("mac", dest, fetch=_fetch_factory(blobs), extract=extract, check=lambda r: None)
    assert (dest / "run").exists()
    assert (dest / engine.MARKER).read_text(encoding="utf-8") == engine.ENGINE["version"]
    assert seen["names"] == ["a.001", "a.002"]


def test_extract_joins_parts_then_runs_tar_on_a_file(tmp_path, monkeypatch):
    cmds = []

    def fake_run(argv, **kw):
        cmds.append(argv)
        assert Path(argv[2]).read_bytes() == b"AABB"  # random-access file, parts in order

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    p1, p2 = tmp_path / "a.001", tmp_path / "a.002"
    p1.write_bytes(b"AA")
    p2.write_bytes(b"BB")
    into = tmp_path / "out"
    into.mkdir()
    engine.extract_archive([p1, p2], into, engine.tar_command("mac"))
    assert cmds[0][0] == "/usr/bin/tar" and cmds[0][1] == "-xf" and cmds[0][3:] == ["-C", str(into)]
    assert not (tmp_path / "engine.7z").exists()
    assert engine.tar_command("linux") == "bsdtar"


def test_cpu_flavor_rejected_on_mac(tmp_path):
    with pytest.raises(ValueError):
        engine.install_engine("mac", tmp_path / "d", flavor="linux-cpu")


def test_swap_restores_old_install_on_failure(tmp_path, monkeypatch):
    dest = tmp_path / "voicevox_engine"
    dest.mkdir()
    (dest / "keep").write_text("old", encoding="utf-8")
    new = tmp_path / "new"
    new.mkdir()
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    real = engine.os.replace
    n = {"i": 0}

    def flaky(a, b):
        n["i"] += 1
        if n["i"] == 2:
            raise OSError("boom")
        real(a, b)

    monkeypatch.setattr(engine.os, "replace", flaky)
    with pytest.raises(OSError):
        engine._swap_in(new, dest, tmp)
    assert (dest / "keep").read_text(encoding="utf-8") == "old"


def test_swap_replaces_symlinked_dest(tmp_path):
    real_old = tmp_path / "real_old"
    real_old.mkdir()
    dest = tmp_path / "voicevox_engine"
    dest.symlink_to(real_old)
    new = tmp_path / "new"
    new.mkdir()
    (new / "run").write_text("n", encoding="utf-8")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    engine._swap_in(new, dest, tmp)
    assert not dest.is_symlink() and (dest / "run").read_text(encoding="utf-8") == "n"
    assert real_old.exists()


@pytest.mark.integration
def test_real_7z_multipart_roundtrip(tmp_path):
    import shutil as sh
    import subprocess as sp

    sevenz = sh.which("7z") or sh.which("7za") or sh.which("7zz")
    if not sevenz:
        pytest.skip("no 7z binary to build a fixture")
    src = tmp_path / "src" / "voicevox_engine"
    src.mkdir(parents=True)
    (src / "run").write_bytes(b"x" * 5000)
    sp.run([sevenz, "a", "-v2k", str(tmp_path / "e.7z"), str(src.parent / "voicevox_engine")], check=True, capture_output=True)
    parts = sorted(tmp_path.glob("e.7z.*"))
    assert len(parts) > 1
    out = tmp_path / "work" / "out"
    out.mkdir(parents=True)
    engine.extract_archive(parts, out, engine.tar_command("linux"))
    assert (out / "voicevox_engine" / "run").stat().st_size == 5000


def test_install_engine_end_to_end_with_real_extract(tmp_path, monkeypatch):
    blobs = {"http://x/a.001": b"aa", "http://x/a.002": b"bb"}
    _patch(monkeypatch, blobs)

    def fake_run(argv, **kw):
        assert Path(argv[2]).read_bytes() == b"aabb"
        root = Path(argv[argv.index("-C") + 1]) / "voicevox_engine"
        root.mkdir()
        (root / "run").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "run").chmod(0o755)

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    dest = tmp_path / "d" / "voicevox_engine"
    engine.install_engine("mac", dest, fetch=_fetch_factory(blobs), check=lambda r: None)
    assert (dest / "run").exists() and (dest / engine.MARKER).read_text(encoding="utf-8") == engine.ENGINE["version"]
    assert [p.name for p in dest.parent.iterdir()] == ["voicevox_engine"]


def test_progress_reports_bytes_across_parts_then_stages(tmp_path, monkeypatch):
    blobs = {"http://x/a.001": b"aaa", "http://x/a.002": b"bb"}
    _patch(monkeypatch, blobs)

    def fetch(url, dest, report):
        Path(dest).write_bytes(blobs[url])
        report(len(blobs[url]), len(blobs[url]))

    seen = []
    engine.install_engine("mac", tmp_path / "voicevox_engine", fetch=fetch, extract=_extract_ok,
                          check=lambda r: None, progress=lambda *a: seen.append(a))
    # two parts: the overall total is unknown, but the byte count keeps adding up
    assert seen == [("download", 3, None), ("download", 5, None), ("extract", 0, 0), ("check", 0, 0)]


def test_progress_single_part_knows_the_total(tmp_path, monkeypatch):
    blobs = {"http://x/a.001": b"abcd"}
    _patch(monkeypatch, blobs)

    def fetch(url, dest, report):
        Path(dest).write_bytes(blobs[url])
        report(2, 4)
        report(4, 4)

    seen = []
    engine.install_engine("mac", tmp_path / "voicevox_engine", fetch=fetch, extract=_extract_ok,
                          check=lambda r: None, progress=lambda *a: seen.append(a))
    assert seen[:2] == [("download", 2, 4), ("download", 4, 4)]


# ---- Windows: the DirectML .vvpp, a plain ZIP with run.exe at its root ----

def _vvpp(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_windows_pin_is_the_single_directml_vvpp():
    ((url, sha),) = engine.ENGINE["windows"]
    assert url.endswith("/0.26.0/voicevox_engine-windows-directml-0.26.0.vvpp")
    assert sha == "7c2f85dfb88160c902297e5d7002ce781a48167e7f28def7a1f10f11390f9bec"


def test_windows_install_unpacks_the_vvpp_with_zipfile(tmp_path, monkeypatch):
    blob = _vvpp({"run.exe": b"MZ", "engine_manifest.json": b"{}",
                  "engine_internal/lib.dll": b"dll", "../escape.txt": b"no"})
    url = "http://x/voicevox_engine-windows-directml-0.26.0.vvpp"
    monkeypatch.setitem(engine.ENGINE, "windows", ((url, _sha(blob)),))

    def no_tar(*a, **kw):
        raise AssertionError("no tar/7z on Windows")

    monkeypatch.setattr(engine.subprocess, "run", no_tar)
    checked = []
    dest = tmp_path / "Local" / "zundamon-jarvis" / "voicevox_engine"
    engine.install_engine("windows", dest, fetch=_fetch_factory({url: blob}), check=checked.append)
    assert checked and checked[0].name == "run.exe"
    assert (dest / "run.exe").read_bytes() == b"MZ"
    assert (dest / "engine_internal" / "lib.dll").read_bytes() == b"dll"
    assert (dest / engine.MARKER).read_text(encoding="utf-8") == engine.ENGINE["version"]
    # zipfile drops ".." from member paths: nothing lands outside the engine directory
    assert [p.parent for p in tmp_path.rglob("escape.txt")] == [dest]
    assert [p.name for p in dest.parent.iterdir()] == ["voicevox_engine"]  # .vvpp and tmp gone


def test_windows_install_rejects_an_archive_without_run_exe(tmp_path, monkeypatch):
    blob = _vvpp({"run": b"#!/bin/sh\n"})  # a Linux layout is not a Windows engine
    url = "http://x/e.vvpp"
    monkeypatch.setitem(engine.ENGINE, "windows", ((url, _sha(blob)),))
    dest = tmp_path / "voicevox_engine"
    with pytest.raises(RuntimeError, match="run.exe"):
        engine.install_engine("windows", dest, fetch=_fetch_factory({url: blob}),
                              check=lambda r: None)
    assert not dest.exists()


def test_windows_installed_needs_run_exe_but_no_execute_bit(tmp_path):
    dest = tmp_path / "voicevox_engine"
    dest.mkdir()
    (dest / "run.exe").write_bytes(b"MZ")  # mode 0644: Windows has no execute bit
    (dest / engine.MARKER).write_text(engine.ENGINE["version"], encoding="utf-8")
    called = []
    engine.install_engine("windows", dest, fetch=lambda *a: called.append(a), check=lambda r: None)
    assert called == []
    assert not engine._installed(dest, "linux")  # no `run` (and not executable) elsewhere


def test_archive_root_finds_run_at_the_root_or_in_one_folder(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "run.exe").write_bytes(b"MZ")
    (flat / "model").mkdir()
    assert engine.archive_root(flat, "windows") == flat
    nested = tmp_path / "nested"
    (nested / "voicevox_engine").mkdir(parents=True)
    (nested / "voicevox_engine" / "run").write_text("x", encoding="utf-8")
    assert engine.archive_root(nested, "linux") == nested / "voicevox_engine"
    with pytest.raises(RuntimeError, match="run.exe"):
        engine.archive_root(nested, "windows")


def test_extract_archive_still_uses_tar_for_7z_parts(tmp_path, monkeypatch):
    cmds = []
    monkeypatch.setattr(engine.subprocess, "run", lambda argv, **kw: cmds.append(argv))
    part = tmp_path / "e.7z.001"
    part.write_bytes(b"7z")
    into = tmp_path / "out"
    into.mkdir()
    engine.extract_archive([part], into, "bsdtar")
    assert cmds and cmds[0][0] == "bsdtar"


def test_check_runs_hides_the_console_window_only_where_there_is_one(monkeypatch):
    seen = []
    monkeypatch.setattr(engine.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))
    engine.check_runs(Path("C:/e/run.exe"))
    argv, kw = seen[0]
    assert argv[1:] == ["--help"]
    assert kw["creationflags"] == getattr(engine.subprocess, "CREATE_NO_WINDOW", 0)
    monkeypatch.setattr(engine.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert engine._no_window() == {"creationflags": 0x08000000}
