"""Which tool calls need a spoken yes/no question before they run, and how to ask.

Pure functions only. The brain asks the orchestrator through a PermissionRequest
whenever ``needs_confirmation`` is True; everything else runs silently.

Local work never asks: any shell command, file edits anywhere, Monitor, local built-ins.
Only these ask: sending mail or messages to people, calendar changes, deleting files or
data, money, sudo, force pushes, and the built-ins that act remotely for the user.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from zundamon_jarvis.text import speakable

Confirmation = Literal["yes", "no", "unclear"]


# ---- Bash / Monitor: find a command that deletes, escalates or force-pushes ----

@dataclass(frozen=True)
class _Finding:
    kind: Literal["delete", "sudo", "force_push", "unknown"]
    cmd: str
    args: tuple[str, ...] = ()
    sub: str = ""  # git subcommand


_PRIVILEGE = frozenset("sudo doas pkexec run0 su".split())
_DELETE_COMMANDS = frozenset("rm rmdir shred unlink srm trash trash-put trash-rm trash-empty truncate wipefs".split())
_SHELLS = frozenset("bash sh zsh dash ksh mksh ash fish".split())
_KEYWORDS = frozenset("if then else elif fi do done while until for in case esac select function ! { } [[ ]] "
                      "coproc".split())
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_MAX_DEPTH = 10


def _opts(s: str) -> frozenset[str]:
    return frozenset(s.split())


# Commands that run another command: (options that take a value, leading positionals to skip).
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "env": (_opts("-u --unset -C --chdir"), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (_opts("-a"), 0),
    "nohup": (frozenset(), 0),
    "time": (_opts("-f --format -o --output"), 0),
    "nice": (_opts("-n --adjustment"), 0),
    "timeout": (_opts("-s --signal -k --kill-after"), 1),
    "xargs": (_opts("-I -n -P -L -l -d -E -e -s -a --arg-file --delimiter --max-args --max-procs "
                    "--replace --max-lines --eof"), 0),
    "stdbuf": (_opts("-i -o -e"), 0),
    "ionice": (_opts("-c -n -p -P -u --class --classdata"), 0),
    "chrt": (frozenset(), 1),
    "taskset": (frozenset(), 1),
    "setsid": (frozenset(), 0),
    "unbuffer": (frozenset(), 0),
    "watch": (_opts("-n --interval"), 0),
    "flock": (_opts("-w --timeout -E --conflict-exit-code"), 1),
    "chroot": (_opts("--userspec --groups"), 1),
    "busybox": (frozenset(), 0),
    "strace": (_opts("-o -e -p -s -u"), 0),
    "ltrace": (_opts("-o -e -p -s -u"), 0),
    "ssh": (_opts("-b -c -D -E -e -F -I -i -J -L -l -m -O -o -p -Q -R -S -W -w"), 1),
}
_PRIV_OPTS = _opts("-u -g -h -p -C -D -r -t -U -T --user --group --host --prompt")


def _paren_end(s: str, k: int) -> int:
    """Index of the ")" closing a "$(" whose body starts at ``k`` (len(s) if unclosed)."""
    depth, n = 1, len(s)
    while k < n:
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "'":
            j = s.find("'", k + 1)
            k = n if j < 0 else j + 1
            continue
        if c == '"':
            k += 1
            while k < n and s[k] != '"':
                if s[k] == "\\":
                    k += 1
                elif s.startswith("$(", k):
                    k = _paren_end(s, k + 2)
                k += 1
            k += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return n


def _lex(s: str) -> list[tuple[str, str]]:
    """Shell-ish tokens: ("w", word) with quotes/escapes removed, ("op", sep), ("redir", op),
    and ("sub", body) for a $(...) or `...` inside double quotes.

    Every way bash starts a new command (; & && || | newline ( ) $( `) is an "op".
    Single-quoted text is never a command. Never fails: an unterminated quote runs to the end.
    """
    out: list[tuple[str, str]] = []
    word: list[str] = []
    started = False

    def end() -> None:
        nonlocal started
        if started:
            out.append(("w", "".join(word)))
        word.clear()
        started = False

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r":
            end()
            i += 1
        elif c == "\n":
            end()
            out.append(("op", ";"))
            i += 1
        elif c == "\\":
            if s[i + 1:i + 2] != "\n":  # backslash-newline is a line continuation
                word.append(s[i + 1:i + 2])
                started = True
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            word.append(s[i + 1:j])
            started = True
            i = j + 1
        elif c == '"':
            j, part = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\" and s[j + 1:j + 2] in ('"', "\\", "`", "$"):
                    part.append(s[j + 1])
                    j += 2
                elif s.startswith("$(", j):
                    e = _paren_end(s, j + 2)
                    out.append(("sub", s[j + 2:e]))
                    part.append(s[j:e + 1])
                    j = e + 1
                elif s[j] == "`":
                    e = s.find("`", j + 1)
                    e = n if e < 0 else e
                    out.append(("sub", s[j + 1:e]))
                    part.append(s[j:e + 1])
                    j = e + 1
                else:
                    part.append(s[j])
                    j += 1
            word.append("".join(part))
            started = True
            i = j + 1
        elif c in "<>" or (c == "&" and s[i + 1:i + 2] == ">"):
            if started and "".join(word).isdigit():  # "2>": the fd number is not a word
                word.clear()
                started = False
            end()
            j = i + 1
            while j < n and s[j] in "<>":
                j += 1
            if j < n and s[j] in "&|":
                j += 1
            if j < n and s[j] == "-" and s.startswith("<<", i):
                j += 1
            out.append(("redir", s[i:j]))
            i = j
        elif c == "$" and s[i + 1:i + 2] in ("'", '"'):  # $'...' / $"...": the quote follows
            i += 1
        elif c == "$" and s[i + 1:i + 2] == "(":
            end()
            out.append(("op", "("))
            i += 2
        elif c in ";&|()`":
            end()
            j = i + 1
            if c in ";&|" and j < n and s[j] in ";&|":
                j += 1
            out.append(("op", s[i:j]))
            i = j
        else:
            word.append(c)
            started = True
            i += 1
    end()
    return out


def _cmd_name(word: str) -> str:
    if word.startswith(("/", "./", "../", "~")):
        return word.rstrip("/").rsplit("/", 1)[-1]
    return word


def _unwrap(args: tuple[str, ...], opts: frozenset[str], npos: int) -> tuple[str, ...]:
    """The command a wrapper runs: skip its options, assignments and leading positionals."""
    i = skipped = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if a.startswith("-") and len(a) > 1:
            i += 2 if a in opts else 1
        elif _ASSIGN.match(a):
            i += 1
        elif skipped < npos:
            skipped += 1
            i += 1
        else:
            break
    return args[i:]


def _git(args: tuple[str, ...]) -> _Finding | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    longs = {a.split("=", 1)[0] for a in rest if a.startswith("--")}
    shorts = set("".join(a[1:] for a in rest if a.startswith("-") and not a.startswith("--")))
    positional = [a for a in rest if not a.startswith("-")]
    delete = _Finding("delete", "git", rest, sub)
    if sub == "clean":
        return delete
    if sub == "reset" and "--hard" in longs:
        return delete
    if sub == "branch" and ("D" in shorts or (("d" in shorts or "--delete" in longs)
                                              and ("f" in shorts or "--force" in longs))):
        return delete
    if sub == "push":
        # "+" at the start of a refspec, or of its destination after ":", forces that ref.
        if ("f" in shorts or longs & {"--force", "--force-with-lease", "--force-if-includes", "--mirror"}
                or any(p.startswith("+") or p.partition(":")[2].startswith("+") for p in positional)):
            return _Finding("force_push", "git", rest, sub)
        if "d" in shorts or "--delete" in longs or any(p.startswith(":") and len(p) > 1 for p in positional):
            return delete
    return None


def _analyze(argv: list[str] | tuple[str, ...], depth: int) -> _Finding | None:
    """One simple command (no operators left)."""
    i = 0
    while i < len(argv) and (argv[i] in _KEYWORDS or _ASSIGN.match(argv[i])):
        i += 1
    if i >= len(argv):
        return None
    cmd, args = _cmd_name(argv[i]), tuple(argv[i + 1:])
    if cmd in _PRIVILEGE:
        return _Finding("sudo", cmd, args)
    if cmd == "command" and args and args[0] in ("-v", "-V"):
        return None  # only looks the command up
    if cmd == "env":
        for k, a in enumerate(args):
            if a in ("-S", "--split-string") and k + 1 < len(args):
                return _scan(args[k + 1], depth + 1)
            if a.startswith(("-S", "--split-string=")) and a not in ("-S", "--split-string"):
                return _scan(a.split("=", 1)[1] if a.startswith("--") else a[2:], depth + 1)
    if cmd in _SHELLS or cmd == "flock":  # the string after -c is a command line
        for k, a in enumerate(args[:-1]):
            if a == "--command" or (a.startswith("-") and not a.startswith("--") and "c" in a):
                return _scan(args[k + 1], depth + 1)
        if cmd in _SHELLS:
            return None
    if cmd in ("ssh", "watch"):  # they join their arguments into one shell command line
        return _scan(" ".join(_unwrap(args, *_WRAPPERS[cmd])), depth + 1)
    if cmd in _WRAPPERS:
        return _analyze(_unwrap(args, *_WRAPPERS[cmd]), depth)
    if cmd == "eval":
        return _scan(" ".join(args), depth + 1)
    if cmd in _DELETE_COMMANDS or cmd == "mkfs" or cmd.startswith("mkfs."):
        return _Finding("delete", cmd, args)
    if cmd == "gio" and args and args[0] in ("trash", "remove", "rm"):
        return _Finding("delete", cmd, args)
    if cmd == "dd" and any(a.startswith("of=") for a in args):
        return _Finding("delete", cmd, args)
    if cmd == "find":
        if "-delete" in args:
            return _Finding("delete", cmd, args)
        for j, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                sub: list[str] = []
                for b in args[j + 1:]:
                    if b in (";", "+"):
                        break
                    sub.append(b)
                found = _analyze(sub, depth)
                if found:
                    return _Finding("delete", cmd, args) if found.kind == "delete" else found
        return None
    if cmd == "git":
        return _git(args)
    return None


def _scan(command: str, depth: int = 0) -> _Finding | None:
    """The first thing in ``command`` that needs a spoken confirmation, or None."""
    if depth > _MAX_DEPTH:
        return _Finding("unknown", "")
    segments: list[list[str]] = [[]]
    redirect_target = False
    for kind, text in _lex(command):
        if kind == "op":
            segments.append([])
            redirect_target = False
            continue
        if kind == "redir":
            redirect_target = True
            continue
        if kind == "sub":  # "$(...)" or "`...`" inside double quotes
            found = _scan(text, depth + 1)
            if found:
                return found
            continue
        if redirect_target:  # "> file" writes a file; that is not deleting
            redirect_target = False
            continue
        segments[-1].append(text)
    for seg in segments:
        found = _analyze(seg, depth)
        if found:
            return found
    return None


# ---- MCP tools: classified by the verbs in the tool's own name ----

_SEND_VERBS = frozenset("send reply forward share post publish invite notify broadcast announce".split())
_MESSAGE_OBJECTS = frozenset("comment comments message messages chat reaction".split())
_MESSAGE_WRITES = frozenset("create add update edit write set schedule".split())
_NOT_MESSAGES = frozenset("label labels draft drafts".split())  # Gmail labels/drafts stay local
_CAL_WRITES = frozenset("create update delete respond cancel move add insert patch remove accept decline rsvp "
                        "reschedule quick import".split())
_DELETE_VERBS = frozenset("delete trash remove drop destroy purge erase wipe discard reset".split())
_PAY_VERBS = frozenset("pay purchase buy checkout charge refund payout transfer withdraw deposit donate "
                       "subscribe".split())
_PAY_OBJECTS = frozenset("payment payments invoice invoices order orders subscription subscriptions billing "
                         "transaction transactions".split())
_PAY_WRITES = frozenset("create update send submit cancel approve post put patch place issue add execute confirm "
                        "complete capture delete".split())
_FREEE_WRITES = frozenset("post put patch delete".split())
# SQL can delete data; EXPLAIN ANALYZE runs the statement; a finished migration/tuning applies SQL to main.
_MCP_SQL = frozenset({"run_sql", "run_sql_transaction", "explain_sql_statement",
                      "complete_database_migration", "complete_query_tuning"})
# Neon: these overwrite a branch's current data.
_MCP_DESTRUCTIVE = frozenset({"restore_snapshot", "finalize_branch_restore"})

McpCategory = Literal["money", "sql", "calendar", "delete", "send"]


def _name_tokens(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if t]


def _mcp_parts(tool: str) -> tuple[str, str] | None:
    parts = tool.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp" or not parts[2]:
        return None
    return parts[1], parts[2]


def _mcp_category(server: str, tool_part: str) -> McpCategory | None:
    tokens = set(_name_tokens(tool_part))
    low = server.lower()
    if ("freee" in low and tokens & _FREEE_WRITES) or tokens & _PAY_VERBS \
            or (tokens & _PAY_OBJECTS and tokens & _PAY_WRITES):
        return "money"
    if tool_part in _MCP_SQL or ("sql" in tokens and tokens & {"run", "execute", "exec"}):
        return "sql"
    if ("calendar" in low or tokens & {"calendar", "event", "events"}) and tokens & _CAL_WRITES:
        return "calendar"
    if tokens & _DELETE_VERBS or tool_part in _MCP_DESTRUCTIVE:
        return "delete"
    if tokens & _SEND_VERBS or (tokens & _MESSAGE_OBJECTS and tokens & _MESSAGE_WRITES
                                and not tokens & _NOT_MESSAGES):
        return "send"
    return None


# ---- the decision ----

_OUTWARD_BUILTINS = frozenset({"RemoteTrigger", "PushNotification"})
_DELETING_BUILTINS = frozenset({"Artifact", "ArtifactData"})  # only their action "delete"


def needs_confirmation(tool: str, input: dict, repo_root: Path | None = None) -> bool:
    """True if this call must be confirmed by voice. ``repo_root`` is no longer used."""
    if not isinstance(input, dict):
        input = {}
    mcp = _mcp_parts(tool)
    if mcp is not None:
        return _mcp_category(*mcp) is not None
    if tool.startswith("mcp__"):
        return True  # malformed MCP name: cannot tell what it does
    if tool in ("Bash", "Monitor"):
        cmd = input.get("command")
        return isinstance(cmd, str) and _scan(cmd) is not None
    if tool in _OUTWARD_BUILTINS:
        return True
    if tool in _DELETING_BUILTINS:
        return input.get("action") == "delete"
    return False


# ---- describe_action: one spoken question ----

def _clean(text: object) -> str:
    """User-supplied text made safe to read aloud."""
    s = speakable(str(text))
    s = re.sub(r"[_/\\*`#|<>{}]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _spoken_when(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return ""
    day = f"{dt.month}月{dt.day}日"
    if "T" not in value:
        return day  # all-day event
    if dt.minute == 0:
        clock = f"{dt.hour}時"
    elif dt.minute == 30:
        clock = f"{dt.hour}時半"
    else:
        clock = f"{dt.hour}時{dt.minute}分"
    return f"{day}の{clock}"


def _service_name(server: str) -> str:
    server = server.removeprefix("claude_ai_")
    return _clean(server.replace("_", " ").replace("-", " ")) or "外部サービス"


_VERB_WORDS = {
    "create": "作成", "update": "更新", "delete": "削除", "send": "送信", "reply": "返信",
    "forward": "転送", "trash": "ゴミ箱への移動", "share": "共有", "move": "移動", "post": "書き込み",
    "put": "書き込み", "patch": "書き込み", "respond": "返事", "upload": "アップロード",
    "copy": "コピー", "cancel": "キャンセル", "run": "実行", "execute": "実行", "reset": "リセット",
}
_OBJECT_WORDS = {
    "page": "ページ", "pages": "ページ", "database": "データベース", "databases": "データベース",
    "branch": "ブランチ", "project": "プロジェクト", "file": "ファイル", "files": "ファイル",
    "comment": "コメント", "label": "ラベル", "draft": "下書き", "message": "メール", "thread": "メール",
    "snapshot": "スナップショット", "endpoint": "エンドポイント", "role": "ロール", "function": "関数",
}


def _calendar(verb: set[str], inp: dict) -> str | None:
    title = _clean(inp.get("summary") or inp.get("title") or "")
    named = f"「{title}」" if title else "予定"
    when = _spoken_when(inp.get("start") or inp.get("startTime"))
    if "create" in verb or "add" in verb:
        at = f"{when}から" if when and "時" in when else (f"{when}に" if when else "")
        return f"{at}{named}をカレンダーに入れるけど、いい？"
    if verb & {"delete", "cancel", "remove"}:
        return f"{named}の予定を消すけど、いい？" if title else "カレンダーの予定を消すけど、いい？"
    if "update" in verb or "move" in verb:
        return f"{named}の予定を変えるけど、いい？" if title else "カレンダーの予定を変えるけど、いい？"
    if "respond" in verb:
        return "予定の招待に返事するけど、いい？"
    return None


def _recipients(to: object) -> str:
    items = to if isinstance(to, list) else [to] if to else []
    names = []
    for a in items:
        local = str(a).split("@", 1)[0]
        local = re.sub(r"[^\w]+", " ", local).replace("_", " ").strip()
        if local:
            names.append(local)
    return "さんと".join(names[:3])


def _gmail(verb: set[str], inp: dict) -> str | None:
    subject = _clean(inp.get("subject") or "")
    about = f"「{subject}」の" if subject else ""
    if "send" in verb:
        who = _recipients(inp.get("to"))
        return f"{about}メールを{who + 'さんに' if who else ''}送るけど、いい？"
    if "reply" in verb:
        return f"{about}メールに返信するけど、いい？"
    if "forward" in verb:
        return f"{about}メールを転送するけど、いい？"
    if "trash" in verb:
        return "メールをゴミ箱に入れるけど、いい？"
    if "draft" in verb and "delete" in verb:
        return f"{about}メールの下書きを消すけど、いい？"
    if "label" in verb and "delete" in verb:
        return "メールのラベルを消すけど、いい？"
    return None


def _describe_mcp(server: str, tool_part: str, inp: dict) -> str:
    tokens = _name_tokens(tool_part)
    verb = set(tokens)
    cat = _mcp_category(server, tool_part)
    said = None
    if cat == "calendar":
        said = _calendar(verb, inp)
    elif "gmail" in server.lower():
        said = _gmail(verb, inp)
    if said:
        return said
    svc = _service_name(server)
    obj = next((_OBJECT_WORDS[t] for t in tokens if t in _OBJECT_WORDS), "データ")
    if cat == "money":
        if "freee" in server.lower():
            return f"{svc}のデータを消すけど、いい？" if "delete" in verb else f"{svc}にデータを書き込むけど、いい？"
        return f"{svc}で支払いの操作をするけど、いい？"
    if cat == "sql":
        return f"{svc}でSQLを実行するけど、いい？"
    if cat == "delete":
        if "trash" in verb:
            return f"{svc}の{obj}をゴミ箱に入れるけど、いい？"
        if "reset" in verb:
            return f"{svc}の{obj}をリセットするけど、いい？"
        return f"{svc}の{obj}を消すけど、いい？"
    if cat == "send":
        if verb & {"comment", "comments"}:
            return f"{svc}にコメントを書くけど、いい？"
        if "share" in verb:
            return f"{svc}で{obj if obj != 'データ' else 'ファイル'}を共有するけど、いい？"
        return f"{svc}にメッセージを送るけど、いい？"
    action = next((_VERB_WORDS[t] for t in tokens if t in _VERB_WORDS), "操作")
    return f"{svc}で{action}をするけど、いい？"


def _targets(args: tuple[str, ...], skip_values: tuple[str, ...] = ()) -> list[str]:
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in skip_values:
            i += 2
            continue
        if not a.startswith("-") and a not in ("{}", ";", "+"):
            out.append(a)
        i += 1
    return out


def _describe_bash(command: str) -> str:
    f = _scan(command)
    if f is None or f.kind == "unknown":
        cmd = command.strip()
        head = _clean(Path(cmd.split()[0]).name) if cmd else ""
        return f"「{head}」コマンドを実行するけど、いい？" if head else "コマンドを実行するけど、いい？"
    if f.kind == "sudo":
        what = _clean(" ".join(_unwrap(f.args, _PRIV_OPTS, 0)[:3]))
        return f"{f.cmd} で {what} を実行するけど、いい？" if what else f"{f.cmd} でコマンドを実行するけど、いい？"
    if f.kind == "force_push":
        return "強制プッシュするけど、いい？"
    if f.cmd == "git":
        if f.sub == "clean":
            return "git clean で管理していないファイルを消すけど、いい？"
        if f.sub == "reset":
            return "git reset で手元の変更を捨てるけど、いい？"
        if f.sub == "branch":
            name = next((_clean(a) for a in _targets(f.args) if _clean(a)), "")
            return f"ブランチ {name} を消すけど、いい？" if name else "ブランチを消すけど、いい？"
        return "リモートのブランチを消すけど、いい？"
    if f.cmd == "find":
        return "find で見つけたファイルを消すけど、いい？"
    if f.cmd == "dd":
        return "dd でディスクかファイルを上書きするけど、いい？"
    if f.cmd.startswith("mkfs") or f.cmd == "wipefs":
        return f"{_clean(f.cmd.split('.')[0])} でディスクを初期化するけど、いい？"
    args = f.args[1:] if f.cmd == "gio" else f.args
    raw = _targets(args, ("-s", "--size", "-r", "--reference") if f.cmd == "truncate" else ())
    if any("*" in t for t in raw):
        return f"{f.cmd} でまとめてファイルを消すけど、いい？"
    names = [n for n in (_clean(t) for t in raw) if n][:2]
    target = "と".join(names)
    if f.cmd.startswith("trash") or (f.cmd == "gio" and f.args[0] == "trash"):
        return f"{target or 'ファイル'} をゴミ箱に入れるけど、いい？"
    if f.cmd == "truncate":
        return f"truncate で {target} の中身を消すけど、いい？" if target else "truncate でファイルの中身を消すけど、いい？"
    return f"{f.cmd} で {target} を消すけど、いい？" if target else f"{f.cmd} でファイルを消すけど、いい？"


_FILE_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
                     "NotebookEdit": "notebook_path"}
_BUILTIN_WORDS = {
    "RemoteTrigger": "リモートのエージェントを動かすけど、いい？",
    "PushNotification": "スマホに通知を送るけど、いい？",
}


def describe_action(tool: str, input: dict) -> str:
    if not isinstance(input, dict):
        input = {}
    mcp = _mcp_parts(tool)
    if mcp is not None:
        return _describe_mcp(*mcp, input)
    if tool in ("Bash", "Monitor"):
        return _describe_bash(str(input.get("command") or ""))
    if tool in _BUILTIN_WORDS:
        return _BUILTIN_WORDS[tool]
    if tool == "Artifact" and input.get("action") == "delete":
        return "公開したページを消すけど、いい？"
    if tool == "ArtifactData" and input.get("action") == "delete":
        return "ページのデータを消すけど、いい？"
    if tool in _FILE_WRITE_TOOLS:
        path = input.get(_FILE_WRITE_TOOLS[tool]) or input.get("file_path") or ""
        name = _clean(Path(str(path)).name)
        target = f"「{name}」" if name else "ファイル"
        return f"{target}を書き換えるけど、いい？"
    name = _clean(tool)
    return f"{name}を使うけど、いい？" if name else "この操作をするけど、いい？"


# ---- parse_confirmation ----

_NO_WORDS = ("だめ", "ダメ", "駄目", "やめ", "止め", "待って", "まって", "待て", "いや", "嫌", "いいえ", "ノー",
             "キャンセル", "しないで", "いらない", "要らない", "違う", "ちがう", "ストップ", "中止")
_NO_ASCII = ("no", "nope", "stop", "cancel")
_YES_WORDS = ("うん", "はい", "いいよ", "いいね", "いいです", "お願い", "おねがい", "オッケー", "おっけー",
              "よろしく", "どうぞ", "頼む", "たのむ", "頼んだ", "やって", "了解", "りょうかい", "ええよ",
              "イエス", "いいと思う")
_YES_EXACT = frozenset({"いい", "いいけど", "いいぞ", "いいわ", "ええ", "そう", "そうして"})
_YES_ASCII = ("ok", "okay", "yes", "yeah", "sure", "yep")
_MAX_YES_LEN = 20  # a long reply is a new request, not an answer


def _normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in s if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


def _has_ascii_word(s: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z]){w}(?![a-z])", s) for w in words)


def parse_confirmation(text: str) -> Confirmation:
    s = _normalize(text)
    if not s:
        return "unclear"
    # "no" wins over "yes": a wrong yes runs an irreversible action, a wrong no just asks again.
    if any(w in s for w in _NO_WORDS) or _has_ascii_word(s, _NO_ASCII):
        return "no"
    if s in _YES_EXACT or (len(s) <= _MAX_YES_LEN and (any(w in s for w in _YES_WORDS)
                                                       or _has_ascii_word(s, _YES_ASCII))):
        return "yes"
    return "unclear"
