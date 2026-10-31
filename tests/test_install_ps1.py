"""install.ps1: static checks everywhere; parsing and the non-Windows refusal only where pwsh is installed.

The installer cannot run for real off Windows (it checks the OS first), so most checks read the
script: the structure that keeps a truncated ``irm | iex`` harmless, no ``exit`` that would close
the user's PowerShell, PowerShell 5.1 syntax only, and the commands it runs.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")
LINES = TEXT.splitlines()


def _code(text: str) -> str:
    """The script with comments and string literals blanked (good enough for this script:
    no here-strings, no '#' inside code other than in strings)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and text[j + 1:j + 2] != "'"):
                j += 2 if text[j] == "'" else 1
            out.append("''")
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "`" else 1
            out.append('""')
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


CODE = _code(TEXT)


def test_no_bom_and_utf8():
    # irm hands iex a string: a BOM would arrive as a stray character in front of the code
    assert not SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_everything_runs_from_one_call_on_the_last_line():
    code_lines = [l for l in CODE.splitlines() if l.strip()]
    assert code_lines[0].startswith("function Install-ZundamonJarvis {")
    assert code_lines[-1] == "Install-ZundamonJarvis"
    assert code_lines[-2] == "}"  # the function ends right before the call
    # nothing at the top level besides the function and the call
    depth, top = 0, []
    for line in code_lines[:-1]:
        if depth == 0:
            top.append(line)
        depth += line.count("{") - line.count("}")
    assert depth == 0
    assert top == [code_lines[0]]


def test_exit_only_when_run_as_a_file():
    exits = [l for l in CODE.splitlines() if re.search(r"\bexit\b", l)]
    assert exits == ["        if ($PSCommandPath) { exit 1 }"]


def test_powershell_5_syntax_only():
    for token in ("&&", "||", "??", "?.", "-Parallel", "Get-Error"):
        assert token not in CODE, token


def test_environment_checks():
    assert "AMD64" in TEXT and "PROCESSOR_ARCHITEW6432" in TEXT
    assert "OSVersion" in TEXT and "-lt 10" in TEXT
    assert "Test-Command 'claude'" in TEXT and "https://claude.com/claude-code" in TEXT
    assert "8GB" in TEXT and "8 GB" in TEXT
    assert "$env:ZJ_VERSION" in TEXT and "$env:ZJ_DRY_RUN -eq '1'" in TEXT
    assert "https://api.github.com/repos/$OwnerRepo/releases/latest" in TEXT


def test_installs_uv_and_the_tagged_zip_without_git():
    assert 'irm https://astral.sh/uv/install.ps1 | iex' in TEXT
    m = re.search(r"Invoke-Zj 'uv' @\((.*?)\)\n", TEXT, re.S)
    args = re.findall(r"'([^']*)'|\"([^\"]*)\"", m.group(1))
    assert [a or b for a, b in args] == [
        "tool", "install", "--force", "--python", "3.12",
        "zundamon-jarvis @ https://github.com/$OwnerRepo/archive/refs/tags/$Version.zip",
    ]
    assert "git+" not in TEXT


def test_setup_is_the_last_step_and_downloads_are_left_to_it():
    assert "Invoke-Zj 'zundamon-jarvis' @('setup')" in TEXT
    assert "engine install" not in TEXT and "models download" not in TEXT
    assert TEXT.index("@('setup')") > TEXT.index("tool', 'install'")


def test_failures_name_the_step_and_say_rerun():
    assert "失敗しました: $($state.Step)" in TEXT
    assert "もう一度実行すれば、続きから進みます" in TEXT


def test_messages_are_japanese_like_install_sh():
    steps = re.findall(r"Step '([^']*)'", TEXT)
    assert steps[0] == "環境を確認しています"
    assert "zundamon-jarvis を入れます" in steps and "uv を確認しています" in steps


# ---- with PowerShell ----

PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh is not installed")


@needs_pwsh
def test_parses_cleanly():
    cmd = ("$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
           f"'{SCRIPT}', [ref]$null, [ref]$e) | Out-Null; $e.Count")
    r = subprocess.run([PWSH, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "0", r.stdout + r.stderr


@needs_pwsh
def test_refuses_outside_windows_and_runs_nothing(tmp_path):
    r = subprocess.run([PWSH, "-NoProfile", "-File", str(SCRIPT)], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ZJ_DRY_RUN": "1"})
    assert r.returncode == 1
    assert "Windows 用" in r.stdout + r.stderr
    assert "==> zundamon-jarvis を入れます" not in r.stdout
