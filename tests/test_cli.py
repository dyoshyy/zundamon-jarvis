from zundamon_jarvis import cli


def test_dispatches_subcommands(monkeypatch):
    seen = []
    monkeypatch.setattr("zundamon_jarvis.daemon.main", lambda argv: seen.append(("daemon", argv)) or 0)
    monkeypatch.setattr("zundamon_jarvis.voices.main", lambda argv: seen.append(("voices", argv)) or 0)
    assert cli.main(["daemon", "--start-paused"]) == 0
    assert cli.main(["voices", "--no-play"]) == 0
    assert seen == [("daemon", ["--start-paused"]), ("voices", ["--no-play"])]


def test_usage(capsys):
    assert cli.main([]) == 2
    assert cli.main(["nope"]) == 2
    assert "usage" in capsys.readouterr().err


def test_service_and_engine_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr("zundamon_jarvis.service.install_services", lambda s: seen.append(("install", s)))
    monkeypatch.setattr("zundamon_jarvis.service.uninstall_services", lambda s: seen.append(("uninstall", s)))
    monkeypatch.setattr("zundamon_jarvis.engine.install_engine", lambda s, flavor=None: seen.append(("engine", flavor)))
    assert cli.main(["service", "install"]) == 0
    assert cli.main(["service", "uninstall"]) == 0
    assert cli.main(["engine", "install", "--cpu"]) == 0
    assert [x[0] for x in seen] == ["install", "uninstall", "engine"] and seen[2][1] == "linux-cpu"
    assert cli.main(["service", "bogus"]) == 2


def test_setup_doctor_models_uninstall_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr("zundamon_jarvis.setup_gui.run_gui", lambda: seen.append(("setup", "gui")) or 0)
    monkeypatch.setattr("zundamon_jarvis.setup_wizard.main", lambda argv: seen.append(("setup", argv)) or 0)
    monkeypatch.setattr("zundamon_jarvis.setup_wizard.uninstall_main", lambda argv: seen.append(("uninstall", argv)) or 0)
    monkeypatch.setattr("zundamon_jarvis.doctor.main", lambda argv: seen.append(("doctor", argv)) or 0)
    monkeypatch.setattr("zundamon_jarvis.stt.download_model", lambda: seen.append(("models", None)) or "/cache/m")
    for argv in (["setup"], ["setup", "--cli"], ["doctor"], ["models", "download"], ["uninstall"]):
        assert cli.main(argv) == 0
    assert seen[:2] == [("setup", "gui"), ("setup", [])]
    assert [s[0] for s in seen[2:]] == ["doctor", "models", "uninstall"]
    assert cli.main(["models"]) == 2
    assert cli.main(["setup", "--bogus"]) == 2


def test_models_download_failure_is_reported(monkeypatch, capsys):
    def boom():
        raise OSError("network down")

    monkeypatch.setattr("zundamon_jarvis.stt.download_model", boom)
    assert cli.main(["models", "download"]) == 1
    assert "network down" in capsys.readouterr().err


def test_version_flag(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip().count(".") >= 1
