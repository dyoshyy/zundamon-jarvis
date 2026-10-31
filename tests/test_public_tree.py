"""The tree is meant to be public: no personal data, MIT licence, VOICEVOX credit in the README."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_public_tree_has_no_personal_strings():
    r = subprocess.run(["sh", str(ROOT / "scripts" / "check-public.sh")], cwd=ROOT,
                       capture_output=True, text=True,
                       env={**os.environ, "ZJ_ALLOW_NO_FORBIDDEN": "1"})  # personal patterns: untracked file, if present
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_public_catches_forbidden_and_secret_strings(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    pats = tmp_path / "pats"
    pats.write_text("# synthetic\nforbidden-token-xyz\n", encoding="utf-8")
    (repo / "a.txt").write_text("clean\nhas FORBIDDEN-token-XYZ here\n", encoding="utf-8")
    (repo / "b.txt").write_text("key sk-" "ant-abc123\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    script = str(ROOT / "scripts" / "check-public.sh")
    r = subprocess.run(["sh", script], cwd=repo, capture_output=True, text=True,
                       env={**env, "ZJ_FORBIDDEN_FILE": str(pats)})
    assert r.returncode == 1
    assert "a.txt:2" in r.stdout and "b.txt:1" in r.stdout
    # without a pattern file the check refuses to say "clean"
    r = subprocess.run(["sh", script], cwd=repo, capture_output=True, text=True, env=env)
    assert r.returncode == 2


def test_readme_mentions_voicevox_credit():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "VOICEVOX:ずんだもん" in text
    assert "https://voicevox.hiroshiba.jp/term/" in text
    assert "Silero" in text
    assert "#linux" in text.lower() or "## Linux" in text


def test_license_is_mit():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 dyoshyy" in text
