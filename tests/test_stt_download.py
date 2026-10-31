"""stt.download_model: what it asks huggingface_hub for, and the byte progress it reports."""

import pytest

from zundamon_jarvis import stt


@pytest.fixture
def snapshots(monkeypatch):
    calls = []

    def fake(repo_id, **kw):
        calls.append((repo_id, kw))
        return f"/cache/{repo_id}"

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake)
    return calls


def test_linux_fetches_what_faster_whisper_loads(snapshots):
    assert stt.download_model("linux") == f"/cache/{stt.FASTER_WHISPER_REPO}"
    repo, kw = snapshots[0]
    assert repo == stt.FASTER_WHISPER_REPO
    assert kw["allow_patterns"] == list(stt.FASTER_WHISPER_FILES) and kw["local_files_only"] is False
    assert "tqdm_class" not in kw


def test_faster_whisper_files_match_its_own_download():
    import inspect

    fw = pytest.importorskip("faster_whisper.utils")
    src = inspect.getsource(fw.download_model)
    assert all(f'"{name}"' in src for name in stt.FASTER_WHISPER_FILES)
    assert fw._MODELS[stt.FASTER_WHISPER_MODEL] == stt.FASTER_WHISPER_REPO


def test_mac_fetches_the_mlx_repo_and_local_only_is_passed(snapshots):
    stt.download_model("mac", local_only=True)
    repo, kw = snapshots[0]
    assert repo == stt.MLX_MODEL and kw == {"local_files_only": True}


def test_progress_goes_through_a_tqdm_class(snapshots):
    stt.download_model("windows", progress=lambda d, t: None)
    assert isinstance(snapshots[0][1]["tqdm_class"], type)


def test_byte_progress_reports_the_furthest_byte_bar():
    seen = []
    cls = stt.byte_progress_tqdm(lambda done, total: seen.append((done, total)))
    files = cls(total=2, desc="Fetching 2 files")  # a file counter: not bytes, not reported
    transfer = cls(total=0, unit="B", unit_scale=True)
    written = cls(total=0, unit="B", unit_scale=True)
    files.update(1)
    assert seen == []
    for bar in (transfer, written):
        bar.total = 1000  # how huggingface_hub grows the totals as each file starts
    written.update(300)
    transfer.update(100)
    written.update(700)
    assert seen == [(300, 1000), (300, 1000), (1000, 1000)]
    for bar in (files, transfer, written):
        bar.close()


def test_byte_progress_without_totals_reports_none():
    seen = []
    cls = stt.byte_progress_tqdm(lambda done, total: seen.append((done, total)))
    bar = cls(total=0, unit="B")
    bar.update(5)
    assert seen == [(5, None)]
    bar.close()
