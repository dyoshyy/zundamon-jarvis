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
    run.write_text("#!/bin/sh\n")
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
    (dest / "run").write_text("x")
    (dest / "run").chmod(0o755)
    (dest / engine.MARKER).write_text(engine.ENGINE["version"])
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
    assert (dest / engine.MARKER).read_text() == engine.ENGINE["version"]
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
    (dest / "keep").write_text("old")
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
    assert (dest / "keep").read_text() == "old"


def test_swap_replaces_symlinked_dest(tmp_path):
    real_old = tmp_path / "real_old"
    real_old.mkdir()
    dest = tmp_path / "voicevox_engine"
    dest.symlink_to(real_old)
    new = tmp_path / "new"
    new.mkdir()
    (new / "run").write_text("n")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    engine._swap_in(new, dest, tmp)
    assert not dest.is_symlink() and (dest / "run").read_text() == "n"
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
        (root / "run").write_text("#!/bin/sh\n")
        (root / "run").chmod(0o755)

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    dest = tmp_path / "d" / "voicevox_engine"
    engine.install_engine("mac", dest, fetch=_fetch_factory(blobs), check=lambda r: None)
    assert (dest / "run").exists() and (dest / engine.MARKER).read_text() == engine.ENGINE["version"]
    assert [p.name for p in dest.parent.iterdir()] == ["voicevox_engine"]
