"""hermes-remote-hands: one Hermes brain on a server, hands on the user's own Mac over OpenSSH (e.g. on a tailnet).

No agent, memory or model runs on the Mac. Tools: mac_computer_use (screen, via cua-driver; see mac_cu.py),
mac_shell, mac_file, mac_patch, mac_search, mac_process — small, explicit tools so fast models never have to
remember ssh syntax or quoting. Every call passes Hermes's approval gate. A pre_tool_call hook also routes any
OTHER tool call that reaches for the Mac directly (e.g. `terminal: ssh <mac> …`, its key file, the control
socket, this plugin's settings) through a single-use approval, so the Mac can't be reached around these tools
without the user seeing it. See README.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("plugins.hermes_remote_hands")

# ControlMaster: reuse one connection across calls (~0.55 s handshake -> ~0.05 s). ControlPersist is short so a
# master left over from before the Mac slept doesn't linger; ServerAlive kills a dead one in ~30 s.
_SSH_BASE = ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
             "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]


def _ssh_opts() -> list[str]:
    """ssh options, with the ControlMaster socket in a private dir under $HERMES_HOME (``cm-hermes-<hash>``)."""
    cdir = _hermes_home() / "cache" / "ssh"
    with contextlib.suppress(OSError):
        cdir.mkdir(parents=True, exist_ok=True)
        os.chmod(cdir, 0o700)
    return [*_SSH_BASE, "-o", "ControlMaster=auto", "-o", f"ControlPath={cdir}/cm-hermes-%C",
            "-o", "ControlPersist=30"]
_MAX_OUT = 50_000          # chars of stdout/stderr returned to the model
_MAX_READ = 200_000        # bytes for mac_file read
_MAX_WRITE = 5_000_000     # bytes for mac_file write


# ── node config ─────────────────────────────────────────────────────────────

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


_CTX = None  # the PluginContext, set in register(); settings are read through ctx.get_config at call time
_DEFAULTS: dict[str, Any] = {"name": "Mac", "ssh_host": "", "home": "", "driver": "~/.local/bin/cua-driver",
                             "projects_dir": "", "write_roots": [], "shell_grants": "per_command"}
_HOST_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@%+:-]*")
_warned: set[str] = set()


class NotConfigured(ValueError):
    """The plugin's settings are missing or invalid; the message tells the user what to set."""


def _warn_once(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        logger.warning("hermes-remote-hands: %s", msg)


def _clean(cfg: dict[str, Any]) -> dict[str, Any]:
    """Type-check and normalise settings (config values are user-editable text: never trust their shape)."""
    out = dict(_DEFAULTS)
    for k in ("name", "ssh_host", "home", "driver", "projects_dir", "shell_grants"):
        v = cfg.get(k)
        if v is None or v == "":
            continue
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            raise NotConfigured(f"setting {k!r} must be text, got {type(v).__name__}")
        out[k] = str(v).strip()
    roots = cfg.get("write_roots") or []
    if isinstance(roots, str):  # `hermes config set ... write_roots "~/a, ~/b"` stores text
        roots = re.split(r"[,\n]", roots)
    if not isinstance(roots, (list, tuple)):
        raise NotConfigured("setting 'write_roots' must be a list of folders")
    out["write_roots"] = [str(r).strip() for r in roots if isinstance(r, str) and str(r).strip()]
    host = out["ssh_host"]
    if not host:
        raise NotConfigured("hermes-remote-hands is not configured: set the ssh_host setting (your Mac's SSH "
                            "alias) and home — see the plugin README")
    if not _HOST_RE.fullmatch(host):
        raise NotConfigured(f"invalid ssh_host {host!r}")  # never let config smuggle ssh options
    if out["home"]:
        home = posixpath.normpath(out["home"])
        if not home.startswith("/") or home == "/":
            raise NotConfigured("the 'home' setting must be an absolute folder, e.g. /Users/you")
        out["home"] = home
    if out["shell_grants"] not in ("per_command", "per_session"):
        _warn_once(f"unknown shell_grants {out['shell_grants']!r}; using per_command")
        out["shell_grants"] = "per_command"
    for k in ("projects_dir", *out["write_roots"]):
        v = out.get(k, k)
        if v.startswith(str(Path.home()) + "/") and not (out["home"] and v.startswith(out["home"] + "/")):
            _warn_once(f"{v!r} looks like a path on THIS server, not the Mac (quote '~' in `hermes config set`)")
    return out


def _node() -> dict[str, Any]:
    """Node settings: ``plugins.entries.hermes-remote-hands.settings`` (plugin.yaml ``config_schema``) over defaults.

    ``shell_grants``: ``per_command`` (default — a grant covers only that exact command) or ``per_session`` (one
    grant = an unrestricted Mac shell for the conversation: the sensitive-command regex is a keyword list that
    quoting/globbing defeats, so treat it as a hint, not a boundary). ``projects_dir`` and optional ``write_roots``
    are the only folders session-level file grants reach."""
    raw: dict[str, Any] = {}
    if _CTX is not None:
        for k in _DEFAULTS:
            with contextlib.suppress(Exception):
                raw[k] = _CTX.get_config(k, default=None)
    return _clean(raw)


def _configured() -> bool:
    try:
        _node()
        return True
    except Exception:
        return False


_target_cache: dict[tuple[str, float], tuple[str, int]] = {}
_reach: dict[str, tuple[float, bool]] = {}
_reach_lock = threading.Lock()


def _ssh_config_mtime() -> float:
    try:
        return (Path.home() / ".ssh" / "config").stat().st_mtime
    except OSError:
        return 0.0


def _ssh_target(host: str) -> tuple[str, int]:
    """(hostname, port) as ssh resolves the alias; cached until ~/.ssh/config changes (e.g. the Mac's IP moves)."""
    key = (host, _ssh_config_mtime())
    if key in _target_cache:
        return _target_cache[key]
    hostname, port, ok = host, 22, False
    with contextlib.suppress(Exception):
        out = subprocess.run(["ssh", "-G", "--", host], capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL).stdout
        kv = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
        hostname, port, ok = kv.get("hostname", host), int(kv.get("port", "22")), "hostname" in kv
    if ok:  # never cache a failed lookup (it would hide the real IP from the bypass guard)
        _target_cache[key] = (hostname, port)
    return hostname, port


def _reachable() -> bool:
    """check_fn: cheap TCP probe of the Mac's SSH port, cached 20 s. Never raises."""
    try:
        host = _node()["ssh_host"]
        now = time.monotonic()
        with _reach_lock:
            if host in _reach and now - _reach[host][0] < 20:
                return _reach[host][1]
        ok = False
        with contextlib.suppress(OSError):
            with socket.create_connection(_ssh_target(host), timeout=2.0):
                ok = True
        with _reach_lock:
            _reach[host] = (now, ok)
        return ok
    except Exception:
        return False


# ── transport ───────────────────────────────────────────────────────────────

def _env() -> dict[str, str]:
    """ssh needs PATH/HOME (+ an agent socket if present); never hand it provider secrets."""
    return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "SSH_AUTH_SOCK")}


def _q(s: str) -> str:
    """shlex.quote for zsh: shlex leaves a leading `=` bare, and zsh's EQUALS expansion turns `=word` into a
    command path (or an error). Force single quotes then."""
    if s.startswith("="):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return shlex.quote(s)


# Remote supervisor: run the command in its own process group; on timeout TERM then KILL the whole group (so
# children like `sleep` die too and release the pipe), exit 142. Otherwise propagate the command's exit status.
_REAPER = _q(
    '$t=shift; $p=fork; if(!$p){ setpgrp(0,0); exec @ARGV; exit 127 } '
    '$SIG{ALRM}=sub{ kill "TERM",-$p; sleep 1; kill "KILL",-$p; print STDERR "\n__HERMES_TIMEOUT__\n"; exit 142 }; alarm $t; '
    'waitpid($p,0); exit(($? & 127) ? 128+($? & 127) : $? >> 8)')


def _ssh_raw(remote_cmd: str, *, timeout: int, stdin: Optional[bytes] = None) -> tuple[int, bytes, str]:
    """Run *remote_cmd* in the Mac's zsh login shell (so PATH matches the user's Terminal), under a REMOTE
    alarm so a timeout really stops it (ssh -T has no pty, so killing the local client wouldn't).
    Returns (exit_code, stdout BYTES, stderr text). A random sentinel is printed before the command and everything
    up to it is dropped, so anything ~/.zprofile / ~/.zlogin print never leaks into results or breaks parsing."""
    sentinel = f"__HERMES_OUT_{os.urandom(8).hex()}__"
    t = int(timeout)
    q = _q(f"printf '%s\\n' {sentinel}\n{remote_cmd}")
    wrapped = (f"if [ -x /usr/bin/perl ]; then exec /usr/bin/perl -e {_REAPER} {t} /bin/zsh -lc {q}; "
               f"else exec /bin/zsh -lc {q}; fi")
    p = subprocess.run(["ssh", *_ssh_opts(), "--", _node()["ssh_host"], wrapped], input=stdin if stdin is not None else b"",
                       capture_output=True, timeout=t + 15, env=_env())
    code, err = p.returncode, p.stderr.decode("utf-8", "replace")
    if code == 142 and "__HERMES_TIMEOUT__" in err:  # the remote alarm fired (a command may itself exit 142)
        raise subprocess.TimeoutExpired(remote_cmd, t, output=p.stdout, stderr=err)
    out, mark = p.stdout, sentinel.encode() + b"\n"
    if (i := out.find(mark)) != -1:
        out = out[i + len(mark):]
    return code, out, err


def _ssh(remote_cmd: str, *, timeout: int, stdin: Optional[bytes] = None) -> tuple[int, str, str]:
    """`_ssh_raw` with stdout decoded as UTF-8 with replacement — binary output never crashes."""
    code, out, err = _ssh_raw(remote_cmd, timeout=timeout, stdin=stdin)
    return code, out.decode("utf-8", "replace"), err


def _clip(s: str) -> str:
    return s if len(s) <= _MAX_OUT else s[:_MAX_OUT // 2] + f"\n…[{len(s) - _MAX_OUT} chars omitted]…\n" + s[-_MAX_OUT // 2:]


def _home() -> str:
    home = _node().get("home") or ""  # normalised by _clean()
    if not home.startswith("/"):
        raise NotConfigured("hermes-remote-hands needs the absolute 'home' setting (e.g. /Users/you)")
    return home


def _expand(path: str) -> str:
    """Absolute, normalised remote path. Only a leading ``~`` / ``$HOME`` / ``${HOME}`` is expanded (from config,
    in Python); nothing else in the path is ever interpreted. Relative paths are taken from home."""
    home = _home()
    for prefix in ("${HOME}", "$HOME", "~"):
        if path == prefix or path.startswith(prefix + "/"):
            path = home + path[len(prefix):]
            break
    if not path.startswith("/"):
        path = home + "/" + path
    return posixpath.normpath(path)


# Credential stores: reading OR writing them is never covered by a session grant (fresh single-use prompt), and
# mac_search never descends into them. One list feeds both the regex and ripgrep's exclude globs.
_SECRET_PARTS = (
    ".ssh", ".gnupg", ".aws", ".kube", ".azure", ".config/gcloud", ".config/gh", ".hermes", ".terraform.d",
    ".docker/config.json", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".vault-token", ".cargo/credentials",
    ".cargo/credentials.toml", ".zsh_history", ".bash_history", ".python_history", ".node_repl_history",
    ".psql_history", ".mysql_history", ".zsh_sessions", ".password-store",
    "Library/Keychains", "Library/Cookies", "Library/Mail", "Library/Messages", "Library/Accounts",
    "Library/Safari", "Library/Group Containers", "Library/LaunchAgents", "Library/LaunchDaemons",
    "Library/Application Support/Google/Chrome", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/Firefox",
    "Library/Application Support/Arc", "Library/Application Support/com.apple.TCC",
    "Library/Application Support/1Password", "Library/Application Support/Bitwarden",
)
_SENSITIVE_PATH_RE = re.compile(
    r"/(?:" + "|".join(re.escape(x) for x in _SECRET_PARTS) + r")(/|$)"
    r"|/Library/Containers/com\.apple\."
    # persistence: shell / git / tmux startup files run code later
    r"|/\.(zshenv|zprofile|zshrc|zlogin|zlogout|bashrc|bash_profile|profile|gitconfig|tmux\.conf)$|/\.config/git(/|$)"
    r"|^/+(etc|private|System|Library|usr|bin|sbin|opt|Applications)(/|$)", re.IGNORECASE)
_SEARCH_EXCLUDES = tuple(g for x in _SECRET_PARTS for g in (f"!**/{x}", f"!**/{x}/**")) + (
    "!**/Library/Containers/com.apple.*/**", "!**/.git/**")
# A `.git` component: hooks and config execute code on the user's next git command.
_GIT_INTERNAL_RE = re.compile(r"/\.git(/|$)")


# Commands that exfiltrate, persist, change security settings or reach the network: never covered by a grant.
_SENSITIVE_CMD_RE = re.compile(
    r"\b(security|osascript|launchctl|sudo|su|scp|sftp|rsync|ssh|nc|ncat|socat|curl|wget|crontab|pmset|csrutil|"
    r"spctl|tccutil|dscl|dseditgroup|systemsetup|networksetup|profiles|xattr|codesign)\b|defaults\s+(write|delete)"
    r"|authorized_keys|\.ssh\b|Keychains|LaunchAgents|LaunchDaemons|\.z(shenv|profile|shrc|login)\b"
    r"|\.bash(rc|_profile)\b|Cookies|Login Data|\.aws\b|\.gnupg\b|base64|/dev/tcp", re.IGNORECASE)


_REALPATH_PY = "import os,sys; p=os.path.realpath(sys.argv[1]); print(p if os.path.isabs(p) and '\\n' not in p else '')"


def _resolve(shown: str) -> str:
    """The path the Mac will actually open, every symlink followed — including a DANGLING final symlink
    (`notes -> ../../.zshenv` that doesn't exist yet), which zsh's :A / realpath(3) leave unresolved. Python's
    os.path.realpath walks components with readlink and handles both. Returns "" when it can't be resolved;
    callers treat that as sensitive (fail closed)."""
    with contextlib.suppress(Exception):
        code, out, _e = _ssh(f"python3 -I -c {_q(_REALPATH_PY)} {_q(shown)}", timeout=20)
        real = out.strip("\n")
        if code == 0 and real.startswith("/") and "\n" not in real:
            return posixpath.normpath(real)
    return ""


_roots_cache: dict[tuple, tuple[str, ...]] = {}  # (home, raw roots) -> resolved roots


def _roots() -> tuple[str, ...]:
    """Resolved project roots (projects_dir + optional write_roots): the only places a session grant reaches."""
    node = _node()
    home = _home()
    raw = tuple(str(r) for r in [node.get("projects_dir"), *(node.get("write_roots") or [])] if r)
    key = (home, raw)
    if key not in _roots_cache:
        res, complete = [], True
        for r in raw:
            try:
                real = _resolve(_expand(r))
            except Exception:
                complete = False  # transient (e.g. SSH hiccup): don't cache, retry next call
                continue
            # A root must be strictly inside home's subfolders, never home itself or one of its ancestors
            # (['~'], ['/Users'], ['~/..'] would turn a session grant into a grant on the whole home).
            if not real or real.count("/") < 2 or real == home or home.startswith(real.rstrip("/") + "/"):
                _warn_once(f"ignoring project folder {r!r}: too broad (must be a folder inside home)")
                continue
            res.append(real)
        if not complete:
            return tuple(res)
        _roots_cache[key] = tuple(res)
    return _roots_cache[key]


def _inside(real: str, roots: tuple[str, ...]) -> bool:
    return any(real == r or real.startswith(r + "/") for r in roots)


def _gate_path(tool: str, verb: str, shown: str, detail: str = "", *,
               strict_outside: bool = False) -> tuple[Optional[str], str]:
    """(error or None, resolved path to operate on). Session grants (shared by all mac_* tools) only reach the
    project roots: 'mac_file:read' / 'mac_file:write' inside them, 'mac_file:read:outside' for other reads.
    Single-use prompt for: credential stores / persistence files (as written OR after resolving symlinks),
    unresolvable paths, ANY write outside the roots or into a .git dir, and strict_outside reads."""
    real = _resolve(shown)
    roots = _roots()
    inside = bool(real) and _inside(real, roots)
    why = ""
    if not real:
        why = "could not resolve symlinks"
    elif _SENSITIVE_PATH_RE.search(shown) or _SENSITIVE_PATH_RE.search(real):
        why = "credentials / startup files"
    elif verb == "Write" and _GIT_INTERNAL_RE.search(real):
        why = ".git internals (hooks/config run code)"
    elif not inside and (verb == "Write" or strict_outside):
        why = "outside the project folders"
    if why:
        key, tag = _once_key("mac_file"), f"⚠ {why} — "
    else:
        key = f"mac_file:{verb.lower()}" + ("" if inside else ":outside")
        tag = "" if inside else "(outside project folders) "
    via = f" → {real}" if real and real != shown else ""
    err = _approve(tool, key, f"{tag}{verb} on {_node()['name']}: {shown}{via}{detail}")
    return err, (real or shown)


def _once_key(kind: str) -> str:
    """A rule key no later call will ever match, so a 'session'/'always' answer can't widen the grant."""
    return f"{kind}:once:{os.urandom(8).hex()}"


# ── approvals ───────────────────────────────────────────────────────────────

def _approve(tool: str, rule_key: str, reason: str) -> Optional[str]:
    """None when approved, else a JSON error. Public plugin entry point into Hermes's shared human gate
    (session/permanent grants, CLI prompt, gateway pending, cron policy; fails closed with nobody to ask)."""
    try:
        from tools.approval import request_tool_approval
        res = request_tool_approval(tool, reason, rule_key=rule_key)
    except Exception as e:  # fail CLOSED: an approval-API change must never mean "run unapproved"
        logger.warning("hermes-remote-hands: approval gate unavailable (%s); refusing", e)
        return json.dumps({"error": f"approval gate unavailable ({e}); refusing to act on the Mac"})
    return None if res.get("approved") else json.dumps({"error": res.get("message") or "denied by user"})


def _guard_command(command: str) -> Optional[str]:
    """Hardline blocks + dangerous-pattern prompts, exactly like the terminal tool. Fails closed."""
    try:
        from tools.approval import check_all_command_guards
        res = check_all_command_guards(command, "local")
    except Exception as e:
        logger.warning("hermes-remote-hands: command guard unavailable (%s); refusing", e)
        return json.dumps({"error": f"command guard unavailable ({e}); refusing", "command": command})
    return None if res.get("approved") else json.dumps({"error": res.get("message") or "blocked", "command": command})


def _offline() -> str:
    n = _node()
    return json.dumps({"error": f"{n['name']} ({n['ssh_host']}) is not reachable — asleep, lid closed, "
                                "or off Tailscale. Ask the user to wake it."})


# ── handlers ────────────────────────────────────────────────────────────────

def _command_gate(tool: str, command: str, cwd: str = "") -> Optional[str]:
    """Guard + approval for running *command* (optionally in absolute *cwd*) on the Mac. None when allowed.
    1) Hardline blocks + dangerous patterns (rm -rf, curl|sh, …): the terminal tool's own guard.
    2) Mac approval. Sensitive commands (keychain, osascript, launchctl, network, ~/.ssh, shell rc files…) or a
       sensitive cwd always get a fresh single-use prompt. Others: per exact command (+cwd) by default, or one
       grant for the conversation when the shell_grants setting is "per_session"."""
    if (err := _guard_command(command)) is not None:
        return err
    node = _node()
    where = f" (in {cwd})" if cwd else ""
    real_cwd = _resolve(cwd) if cwd else ""
    if _SENSITIVE_CMD_RE.search(command) or (cwd and (not real_cwd or _SENSITIVE_PATH_RE.search(cwd)
                                                      or _SENSITIVE_PATH_RE.search(real_cwd))):
        key, reason = _once_key("mac_shell"), f"⚠ SENSITIVE command on {node['name']}{where}: {command}"
    elif node.get("shell_grants") == "per_session":
        key, reason = "mac_shell", f"Run on {node['name']}{where}: {command}"
    else:
        key = "mac_shell:" + hashlib.sha256(f"{cwd}\x00{command}".encode()).hexdigest()[:16]
        reason = f"Run on {node['name']}{where}: {command}"
    return _approve(tool, key, reason)


def mac_shell(params: dict, **_kw) -> str:
    try:
        command = str(params.get("command") or "").strip()
        if not command:
            return json.dumps({"error": "command is required"})
        try:
            timeout = max(1, min(int(params.get("timeout") or 60), 600))
        except (TypeError, ValueError):
            timeout = 60
        cwd = str(params.get("cwd") or "").strip()
        cwd = _expand(cwd) if cwd else ""
        if not _reachable():
            return _offline()
        if (err := _command_gate("mac_shell", command, cwd)) is not None:
            return err
        remote = f"cd -- {_q(cwd)} && {{\n{command}\n}}" if cwd else command
        try:
            code, out, errout = _ssh(remote, timeout=timeout)
        except subprocess.TimeoutExpired:
            return json.dumps({"error": f"timed out after {timeout}s (the command may still be running on the Mac)",
                               "command": command})
        if code == 255 and not out:
            return json.dumps({"error": f"ssh failed: {errout.strip()[:500]}", "command": command})
        return json.dumps({"exit_code": code, "stdout": _clip(out), "stderr": _clip(errout)})
    except NotConfigured as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("mac_shell failed")
        return json.dumps({"error": f"mac_shell failed: {e}"})


def mac_file(params: dict, **_kw) -> str:
    try:
        action = str(params.get("action") or "").strip()
        path = str(params.get("path") or "").strip()
        if action not in ("read", "write", "list") or not path:
            return json.dumps({"error": "action must be read|write|list and path is required"})
        if not _reachable():
            return _offline()
        shown = _expand(path)
        if action in ("read", "list"):
            err, real = _gate_path("mac_file", "Read", shown)
            if err is not None:
                return err
            q = _q(real)
            if action == "list":
                code, out, errout = _ssh(f"ls -la -- {q}", timeout=60)
                return json.dumps({"path": shown, "listing": _clip(out)} if code == 0
                                  else {"error": errout.strip() or "list failed", "path": shown})
            if params.get("offset") is not None or params.get("limit") is not None:
                return _read_numbered(real, params)
            code, raw, errout = _ssh_raw(f"head -c {_MAX_READ + 1} -- {q}", timeout=60)
            if code != 0:
                return json.dumps({"error": errout.strip() or "read failed", "path": shown})
            truncated = len(raw) > _MAX_READ
            out = raw[:_MAX_READ].decode("utf-8", "replace")
            if "\x00" in out[:8192]:
                return json.dumps({"error": "binary file — use mac_shell (e.g. `file`, `xxd | head`) instead",
                                   "path": shown})
            return json.dumps({"path": shown, "content": out, "truncated": truncated})
        content = params.get("content")
        if not isinstance(content, str):
            return json.dumps({"error": "content (string) is required for write"})
        data = content.encode("utf-8")
        if len(data) > _MAX_WRITE:
            return json.dumps({"error": f"content is {len(data)} bytes; limit is {_MAX_WRITE}"})
        append = bool(params.get("append"))
        err, real = _gate_path("mac_file", "Write", shown, f" ({len(data)} bytes{', append' if append else ''})")
        if err is not None:
            return err
        q = _q(real)
        op = ">>" if append else ">"
        code, _out, errout = _ssh(f"[ -L {q} ] && {{ echo 'path changed to a symlink; refusing' >&2; exit 3; }}; "
                                  f"mkdir -p -- \"$(dirname -- {q})\" && cat {op} {q}", timeout=60, stdin=data)
        return json.dumps({"ok": True, "path": shown, "bytes": len(data)} if code == 0
                          else {"error": errout.strip() or "write failed", "path": shown})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timed out talking to the Mac"})
    except NotConfigured as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("mac_file failed")
        return json.dumps({"error": f"mac_file failed: {e}"})


# ── dev tools: numbered reads, patch, search, long-running processes ──────

_TOTAL_MARK = "__HERMES_TOTAL_LINES__ "  # printed by awk's END block; rpartition picks the last one
_MAX_PATCH = 5_000_000
_ANSI_RE = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x9b[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)"
                      r"|\x1b[P_^X].*?\x1b\\|\x1b[()][A-Za-z0-9]|\x1b[ -/]*[0-~]", re.DOTALL)


def _clean_term(text: str) -> str:
    """Strip terminal escapes; for \r-redrawn lines (progress bars) keep only the last frame."""
    text = _ANSI_RE.sub("", text)
    return "\n".join(ln.rstrip("\r").rsplit("\r", 1)[-1] for ln in text.split("\n"))
_PROC_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")  # no '.'/':' — tmux target separators
_TMUX = "tmux -L hermes"  # own tmux server: never touches (or is reachable from) the user's own tmux sessions
_LOG_DIR = "~/Library/Logs/hermes-dev"


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


def _read_numbered(shown: str, params: dict) -> str:
    """`N|line` output like the server's read_file (caller already approved the read)."""
    offset = _int(params.get("offset"), 1, 1, 10**9)
    limit = _int(params.get("limit"), 500, 1, 2000)
    end = offset + limit - 1
    # long lines (minified JS) are cut at 2000 chars so 2000 lines can't pull megabytes over the wire
    awk = (f'NR>={offset} && NR<={end} {{ l=$0; if (length(l) > 2000) l = substr(l, 1, 2000) "…[line cut]"; '
           f'print NR "|" l }} END {{ print "{_TOTAL_MARK}" NR }}')
    code, out, errout = _ssh(f"LC_ALL=C /usr/bin/awk {_q(awk)} {_q(shown)}", timeout=60)
    if code != 0:
        return json.dumps({"error": errout.strip() or "read failed", "path": shown})
    if "\x00" in out[:8192]:
        return json.dumps({"error": "binary file — use mac_shell (`file`, `xxd | head`)", "path": shown})
    body, _, tail = out.rpartition(_TOTAL_MARK)
    total = _int(tail.strip(), 0, 0, 10**12)
    resp = {"path": shown, "content": _clip(body), "total_lines": total}
    if end < total:
        resp["next_offset"] = end + 1
    return json.dumps(resp)


def _lint(path: str, text: str) -> Optional[str]:
    """Cheap server-side syntax check of the patched text (advisory, never blocks the write)."""
    try:
        if path.endswith(".py"):
            compile(text, path, "exec")
        elif path.endswith(".json"):
            json.loads(text)
    except SyntaxError as e:
        return f"SyntaxError line {e.lineno}: {e.msg}"
    except ValueError as e:
        return f"JSON error: {e}"
    return None


def _locate(text: str, old: str) -> tuple[list[tuple[int, int]], str]:
    """(match spans, mode). Exact first; then CRLF-normalised; then line-aligned with trailing whitespace ignored
    (the usual copy-paste drift). Never fuzzier than that: a wrong match edits the wrong code. Spans are spliced
    by position, so a fallback match can never re-find (and edit) some other occurrence of the text."""
    def spans(needle: str) -> list[tuple[int, int]]:
        out, i = [], text.find(needle)
        while i != -1:
            out.append((i, i + len(needle)))
            i = text.find(needle, i + len(needle))
        return out
    if (hits := spans(old)):
        return hits, "exact"
    if "\r\n" in text and "\r\n" not in old and (hits := spans(old.replace("\n", "\r\n"))):
        return hits, "crlf"
    ws = " \t\r"  # only these count as trailing whitespace; only \n splits lines (not \f, \u2028, …)
    want = [ln.rstrip(ws) for ln in old.split("\n")]
    if want and want[-1] == "":
        want = want[:-1]
    if not want or not any(want):
        return [], "none"
    lines = [ln for ln in re.split(r"(?<=\n)", text) if ln]
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    found: list[tuple[int, int]] = []
    i = 0
    while i <= len(lines) - len(want):
        if all(lines[i + j].rstrip(ws + "\n") == want[j] for j in range(len(want))):
            end = starts[i] + sum(len(lines[i + j]) for j in range(len(want)))
            if not old.endswith("\n"):  # keep the final line break when old_string didn't include one
                end -= len(lines[i + len(want) - 1]) - len(lines[i + len(want) - 1].rstrip("\r\n"))
            found.append((starts[i], end))
            i += len(want)
        else:
            i += 1
    return found, "whitespace" if found else "none"


def mac_patch(params: dict, **_kw) -> str:
    try:
        path = str(params.get("path") or "").strip()
        old, new = params.get("old_string"), params.get("new_string")
        if not path or not isinstance(old, str) or not isinstance(new, str) or not old:
            return json.dumps({"error": "path, old_string (non-empty) and new_string are required"})
        if old == new:
            return json.dumps({"error": "old_string and new_string are identical"})
        replace_all = bool(params.get("replace_all"))
        if not _reachable():
            return _offline()
        shown = _expand(path)
        err, real = _gate_path("mac_patch", "Write", shown, " (patch)")
        if err is not None:
            return err
        q = _q(real)
        code, raw, errout = _ssh_raw(f"head -c {_MAX_PATCH + 1} -- {q}", timeout=60)
        if code != 0:
            return json.dumps({"error": errout.strip() or "read failed", "path": shown})
        if len(raw) > _MAX_PATCH:
            return json.dumps({"error": f"file larger than {_MAX_PATCH} bytes; edit it with mac_shell", "path": shown})
        if b"\x00" in raw:
            return json.dumps({"error": "binary file; refusing to patch", "path": shown})
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return json.dumps({"error": "file is not UTF-8; refusing to patch (would corrupt it)", "path": shown})
        hits, mode = _locate(text, old)
        if not hits:
            return json.dumps({"error": "old_string not found (re-read the file with mac_file offset/limit and "
                                        "copy the exact text)", "path": shown})
        if len(hits) > 1 and not replace_all:
            lines_at = [text.count("\n", 0, h) + 1 for h, _ in hits[:20]]
            return json.dumps({"error": f"old_string matches {len(hits)} places (lines {lines_at}); add context "
                                        "to make it unique or pass replace_all=true", "path": shown})
        if mode != "exact" and "\r\n" in text and "\r\n" not in new:
            new = new.replace("\n", "\r\n")
        new_text, prev = [], 0
        for a_, b_ in hits:
            new_text += [text[prev:a_], new]
            prev = b_
        new_text = "".join(new_text) + text[prev:]
        digest = hashlib.sha256(raw).hexdigest()
        # Compare-and-swap: stream the new text to a temp file, then overwrite in place only if the file is still
        # byte-identical to what we read. `cat > f` keeps the inode, mode, owner and symlinks.
        remote = (f"f={q}; tmp=$(mktemp \"${{TMPDIR:-/tmp}}/hermes-patch.XXXXXX\") || exit 4; "
                  f"trap 'rm -f \"$tmp\"' EXIT; cat > \"$tmp\" || exit 4; "
                  f"h=$(shasum -a 256 < \"$f\" | cut -c1-64); "
                  f"[ \"$h\" = {digest} ] || {{ echo 'file changed on disk since it was read; retry' >&2; exit 3; }}; "
                  f"[ -L \"$f\" ] && {{ echo 'path changed to a symlink; refusing' >&2; exit 3; }}; cat \"$tmp\" > \"$f\"")
        code, _o, errout = _ssh(remote, timeout=60, stdin=new_text.encode("utf-8"))
        if code != 0:
            return json.dumps({"error": errout.strip() or f"write failed (exit {code})", "path": shown})
        import difflib
        diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                            fromfile=f"a{shown}", tofile=f"b{shown}", n=3))
        resp = {"ok": True, "path": shown, "replacements": len(hits) if replace_all else 1,
                "diff": diff[:20_000] + ("\n…[diff truncated]" if len(diff) > 20_000 else "")}
        if mode != "exact":
            resp["note"] = "matched after normalising line endings / trailing whitespace"
        if (lint := _lint(shown, new_text)):
            resp["lint"] = lint
        return json.dumps(resp)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timed out talking to the Mac"})
    except NotConfigured as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("mac_patch failed")
        return json.dumps({"error": f"mac_patch failed: {e}"})


def mac_search(params: dict, **_kw) -> str:
    try:
        pattern = str(params.get("pattern") or "")
        if not pattern:
            return json.dumps({"error": "pattern is required"})
        target = str(params.get("target") or "content")
        if target not in ("content", "files"):
            return json.dumps({"error": "target must be content|files"})
        shown = _expand(str(params.get("path") or "").strip() or _node().get("projects_dir") or "~")
        limit = _int(params.get("limit"), 50, 1, 500)
        ctx = _int(params.get("context"), 0, 0, 5)
        glob = str(params.get("file_glob") or "")
        if not _reachable():
            return _offline()
        err, real = _gate_path("mac_search", "Read", shown, f" (search {target}: {pattern!r}"
                               f"{', incl. hidden files' if params.get('hidden') else ''})",
                               strict_outside=bool(params.get("hidden")))
        if err is not None:
            return err
        argv = ["rg", "--no-config", "--no-follow", "--color=never"]  # ignore a user ripgreprc (--follow etc.)
        if params.get("hidden"):
            argv.append("--hidden")
        if target == "files":
            argv += ["--files", "-g", pattern]
            cap = limit
        else:
            argv += ["-n", "--no-heading", "--max-columns=400", "--max-columns-preview"]
            if params.get("ignore_case"):
                argv.append("-i")
            if params.get("fixed_strings"):
                argv.append("-F")
            if ctx:
                argv += ["-C", str(ctx)]
            if glob:
                argv += ["-g", glob]
            argv += ["-e", pattern]
            cap = limit * (1 + 2 * ctx) + (limit if ctx else 0)
        for g in _SEARCH_EXCLUDES:  # after the positive globs: later -g wins in ripgrep
            argv += ["-g", g]
        argv += ["--", real]
        remote = ("command -v rg >/dev/null || { echo 'ripgrep (rg) not installed: brew install ripgrep' >&2; exit 2; }; "
                  f"{' '.join(_q(a) for a in argv)} | head -n {cap + 1}; exit ${{pipestatus[1]}}")
        code, out, errout = _ssh(remote, timeout=90)
        # rg: 0 matches, 1 none, 2 error (often just unreadable dirs, WITH results), 141 SIGPIPE when head cut it
        if code not in (0, 1, 2, 141) or (code == 2 and not out.strip()):
            return json.dumps({"error": errout.strip()[:2000] or f"search failed (exit {code})", "path": shown})
        lines = out.splitlines()
        truncated = len(lines) > cap
        resp = {"path": shown, "target": target, "matches": _clip("\n".join(lines[:cap])),
                "count": min(len(lines), cap), "truncated": truncated}
        if code == 2:
            resp["warnings"] = errout.strip()[:1000]
        return json.dumps(resp)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "search timed out; narrow the path or pattern"})
    except NotConfigured as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("mac_search failed")
        return json.dumps({"error": f"mac_search failed: {e}"})


def mac_process(params: dict, **_kw) -> str:
    """Long-running jobs (dev servers, watchers, long builds/tests) in detached tmux sessions on the Mac, named
    `hermes-<name>`; output is tee'd to ~/Library/Logs/hermes-dev/<name>.log, exit status to <name>.status.
    The user can watch live with `tmux -L hermes attach -t hermes-<name>`."""
    try:
        action = str(params.get("action") or "").strip()
        if action not in ("start", "status", "log", "send", "stop", "list"):
            return json.dumps({"error": "action must be start|status|log|send|stop|list"})
        name = str(params.get("name") or "").strip()
        if action != "list" and not _PROC_NAME_RE.match(name):
            return json.dumps({"error": "name is required: 1-40 chars of letters, digits, _ -"})
        if not _reachable():
            return _offline()
        sess = f"hermes-{name}"
        # Force single quotes: zsh expands a bare leading `=word` to a command path, and shlex.quote leaves `=`
        # alone. `=` = exact session-name match in tmux. sess is [A-Za-z0-9_.-] only, so this quoting is safe.
        qs, qp, logd = f"'={sess}'", f"'={sess}:'", _expand(_LOG_DIR)
        log, status = _q(f"{logd}/{name}.log"), _q(f"{logd}/{name}.status")
        node = _node()["name"]
        if action == "list":
            if (err := _approve("mac_process", "mac_file:read", f"Read on {node}: list hermes tmux jobs")) is not None:
                return err
            code, out, _e = _ssh(_TMUX + " ls -F '#{session_name} #{session_created}' 2>/dev/null | grep '^hermes-'; "
                                 f"echo ---; ls -1 {_q(logd)} 2>/dev/null", timeout=30)
            running, _, files = out.partition("---\n")
            return json.dumps({"running": [ln.split()[0][7:] for ln in running.splitlines() if ln.strip()],
                               "logs": sorted({f.rsplit(".", 1)[0] for f in files.split()}), "log_dir": logd})
        if action in ("status", "log"):
            if (err := _approve("mac_process", "mac_file:read", f"Read on {node}: job {name} log")) is not None:
                return err
            n = _int(params.get("lines"), 100, 1, 2000)
            code, out, _e = _ssh(f"if {_TMUX} has-session -t {qs} 2>/dev/null; then echo RUNNING; else echo -n 'EXITED '; "
                                 f"cat {status} 2>/dev/null || echo '?'; fi; echo ---; "
                                 f"tail -n {n if action == 'log' else 15} {log} 2>/dev/null", timeout=30)
            head, _, body = out.partition("---\n")
            state = head.strip()
            resp: dict[str, Any] = {"name": name, "running": state == "RUNNING",
                                    "output": _clip(_clean_term(body))}
            if state.startswith("EXITED"):
                ec = state.split()[-1]
                resp["exit_code"] = int(ec) if ec.lstrip("-").isdigit() else None
            return json.dumps(resp)
        if action == "stop":
            if (err := _approve("mac_process", f"mac_process:stop:{name}", f"Stop job {name} on {node}")) is not None:
                return err
            code, _o, errout = _ssh(f"{_TMUX} has-session -t {qs} 2>/dev/null || {{ echo 'not running' >&2; exit 1; }}; "
                                    f"{_TMUX} send-keys -t {qp} C-c; sleep 2; {_TMUX} kill-session -t {qs} 2>/dev/null; true",
                                    timeout=30)
            return json.dumps({"ok": code == 0, "name": name} if code == 0 else {"error": errout.strip(), "name": name})
        if action == "send":
            text = params.get("input")
            if not isinstance(text, str):
                return json.dumps({"error": "input (string) is required for send"})
            if (err := _guard_command(text)) is not None:
                return err
            n = _node()["name"]
            if _SENSITIVE_CMD_RE.search(text):
                key, reason = _once_key("mac_process"), f"⚠ SENSITIVE input to job {name} on {n}: {text!r}"
            else:
                key = "mac_process:send:" + hashlib.sha256(f"{name}\x00{text}".encode()).hexdigest()[:16]
                reason = f"Type into job {name} on {n}: {text!r}"
            if (err := _approve("mac_process", key, reason)) is not None:
                return err
            enter = "" if params.get("enter") is False else f"; {_TMUX} send-keys -t {qp} Enter"
            code, _o, errout = _ssh(f"{_TMUX} send-keys -t {qp} -l -- {_q(text)}{enter}", timeout=30)
            return json.dumps({"ok": code == 0} if code == 0 else {"error": errout.strip() or "send failed"})
        # start
        command = str(params.get("command") or "").strip()
        if not command:
            return json.dumps({"error": "command is required for start"})
        cwd = _expand(str(params.get("cwd") or "").strip() or "~")
        if (err := _command_gate("mac_process", command, cwd)) is not None:
            return err
        inner = f"{{\n{command}\n}} 2>&1 | tee -a {log}; echo ${{pipestatus[1]}} > {status}"
        remote = (f"export TERM=xterm-256color; command -v tmux >/dev/null || {{ echo 'tmux not installed' >&2; exit 2; }}; "
                  f"{_TMUX} has-session -t {qs} 2>/dev/null && {{ echo 'job {name} is already running; stop it first' >&2; exit 3; }}; "
                  f"mkdir -p {_q(logd)} && rm -f {status} && printf '$ %s\\n' {_q(command)} > {log} && "
                  f"{_TMUX} new-session -d -s {_q(sess)} -x 200 -y 50 -c {_q(cwd)} "
                  f"/bin/zsh -lc {_q(inner)}")
        code, _o, errout = _ssh(remote, timeout=30)
        if code != 0:
            return json.dumps({"error": errout.strip() or f"start failed (exit {code})", "name": name})
        return json.dumps({"ok": True, "name": name, "cwd": cwd, "log": f"{logd}/{name}.log",
                           "hint": f"mac_process(action='log', name='{name}') for output; user can "
                                   f"`tmux -L hermes attach -t {sess}` to watch"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timed out talking to the Mac"})
    except NotConfigured as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("mac_process failed")
        return json.dumps({"error": f"mac_process failed: {e}"})


# ── bypass guard: other tools reaching for the Mac directly ─────────────────

# NOT computer_use: it now drives the SERVER's Bot Screen, whose terminal holds the Mac's SSH key, so typing an
# ssh command there must hit the same approval as the terminal tool.
_OWN_TOOLS = {"mac_shell", "mac_file", "mac_patch", "mac_search", "mac_process", "mac_computer_use"}


_key_cache: dict[tuple[str, float], tuple[str, ...]] = {}


def _identity_files(host: str) -> tuple[str, ...]:
    """Basenames of the private keys ssh would use for *host* (non-default ones only), e.g. ``mac_hands``."""
    key = (host, _ssh_config_mtime())
    if key not in _key_cache:
        names: set[str] = set()
        ok = False
        with contextlib.suppress(Exception):
            out = subprocess.run(["ssh", "-G", "--", host], capture_output=True, text=True, timeout=5,
                                 stdin=subprocess.DEVNULL).stdout
            for line in out.splitlines():
                ok = ok or line.startswith("hostname ")
                if line.startswith("identityfile "):
                    base = posixpath.basename(line.split(" ", 1)[1].strip())
                    if len(base) >= 4 and not base.startswith("id_"):  # default keys are too generic
                        names.add(base)
        if not ok:
            return tuple(sorted(names))
        _key_cache[key] = tuple(sorted(names))
    return _key_cache[key]


# Always guarded, even if the settings are missing or broken: the control socket (`ssh -S <socket> anyhost cmd`
# rides an open connection without naming host or key) and this plugin's own config / code.
_STATIC_MARKERS = ("cm-hermes", "entries.hermes-remote-hands", "plugins/hermes-remote-hands")


def _mac_markers() -> tuple[str, ...]:
    marks: set[str] = set(_STATIC_MARKERS)
    try:
        n = _node()
        hostname, _ = _ssh_target(n["ssh_host"])
        marks |= {n["ssh_host"], hostname, *_identity_files(n["ssh_host"])}
    except Exception:  # broken settings must not switch the guard off: fall back to the raw alias
        with contextlib.suppress(Exception):
            raw = str(_CTX.get_config("ssh_host", default="") or "").strip() if _CTX is not None else ""
            if raw:
                marks.add(raw)
    return tuple(sorted(m.lower() for m in marks if m and len(m) >= 2))


def _mentions(marker: str, text: str) -> bool:
    """Token match: 'macbook' hits `ssh macbook`, `user@macbook:` and `~/.ssh/macbook_key`, not 'umbrella'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])", text) is not None


def _pre_tool_call(tool_name: str = "", args: Any = None, **_kw) -> Optional[dict]:
    """Any non-Mac tool whose arguments mention the Mac (its SSH alias/IP, its key, the control socket, this
    plugin's settings) is escalated to a single-use human approval. Never raises: it must not break other tools."""
    try:
        if tool_name in _OWN_TOOLS or not args:
            return None
        blob = json.dumps(args, default=str).lower()
        flat = re.sub(r"[\\'\"`]|\$\{?\w*\}?", "", blob)  # mac''book, mac\\book, mac${x}book -> macbook
        hit = next((m for m in _mac_markers() if _mentions(m, blob) or _mentions(m, flat)), None)
        if hit is None and re.search(r"\bssh\b.*(\s-s(\s|[~/$])|controlpath)", flat):
            hit = "ssh control socket"
        if hit is None:
            return None
        # Single-use key: one 'session' answer must not open the Mac to every later terminal call.
        return {"action": "approve", "rule_key": f"mac_direct:{tool_name}:{os.urandom(6).hex()}",
                "message": f"`{tool_name}` is reaching the user's Mac directly (matched '{hit}'), "
                           "outside the mac_* tools"}
    except Exception:
        return None


# ── registration ────────────────────────────────────────────────────────────

def register(ctx):
    global _CTX
    _CTX = ctx
    try:
        node = _node()
        if len(node["ssh_host"]) < 4:
            logger.warning("hermes-remote-hands: ssh_host %r is short; other tools' calls that mention it (e.g. "
                           "inside longer words) will ask for approval — a distinctive alias is better",
                           node["ssh_host"])
    except ValueError as e:  # not configured yet: tools stay hidden (check_fn) until settings are valid
        logger.warning("hermes-remote-hands: %s", e)
        node = {"name": "Mac", "projects_dir": ""}
    name = node["name"]
    # check_fn is config-only (no network): the tool list stays stable when the Mac sleeps, so the prompt cache
    # survives, and an offline Mac returns a clear "asleep / off the network" error at call time.
    ctx.register_tool(
        check_fn=_configured, name="mac_shell", toolset="mac", handler=mac_shell, emoji="🍎",
        schema={
            "name": "mac_shell",
            "description": (f"Run a shell command on the user's {name} (macOS, zsh login shell, over SSH). "
                            "Use this for anything on the user's own computer; use the normal terminal tool for "
                            "the server. To click/type in Mac apps use mac_computer_use (computer_use drives "
                            "the SERVER's own screen)."),
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "Shell command, e.g. 'git status' or 'open -a Safari'"},
                "cwd": {"type": "string", "description": "Directory to run in, e.g. ~/Documents/Code/myrepo"},
                "timeout": {"type": "integer", "description": "Seconds (default 60, max 600). For dev servers, "
                                                              "watchers or longer jobs use mac_process"},
            }, "required": ["command"]},
        })
    ctx.register_tool(
        check_fn=_configured, name="mac_file", toolset="mac", handler=mac_file, emoji="🍎",
        schema={
            "name": "mac_file",
            "description": (f"Read, write or list text files on the user's {name}. Paths may start with ~. "
                            "For code, pass offset/limit to get 'N|line' numbered output (then edit with "
                            "mac_patch). For server files use read_file/write_file instead."),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["read", "write", "list"]},
                "path": {"type": "string", "description": "e.g. ~/Desktop/notes.txt"},
                "content": {"type": "string", "description": "Text to write (action=write)"},
                "append": {"type": "boolean", "description": "Append instead of overwrite (action=write)"},
                "offset": {"type": "integer", "description": "read: first line, 1-indexed (numbered output)"},
                "limit": {"type": "integer", "description": "read: max lines (default 500, max 2000)"},
            }, "required": ["action", "path"]},
        })
    projects = node.get("projects_dir") or "~"
    ctx.register_tool(
        check_fn=_configured, name="mac_patch", toolset="mac", handler=mac_patch, emoji="🍎",
        schema={
            "name": "mac_patch",
            "description": (f"Edit a text file on the user's {name} by exact find-and-replace (like patch, for the "
                            "Mac). old_string must match exactly once unless replace_all=true; include enough "
                            "surrounding lines to be unique. Returns a unified diff. Refuses if the file changed "
                            "on disk since it was read. Create new files with mac_file write."),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": f"e.g. {projects}/myrepo/src/app.ts"},
                "old_string": {"type": "string", "description": "Exact text to replace (no line-number prefixes)"},
                "new_string": {"type": "string", "description": "Replacement text ('' deletes)"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
            }, "required": ["path", "old_string", "new_string"]},
        })
    ctx.register_tool(
        check_fn=_configured, name="mac_search", toolset="mac", handler=mac_search, emoji="🍎",
        schema={
            "name": "mac_search",
            "description": (f"Search files on the user's {name} with ripgrep (respects .gitignore). "
                            "target='content': regex search inside files, returns path:line:text. "
                            "target='files': find files whose name matches the glob in pattern (e.g. '*.tsx'). "
                            f"Default path: {projects}."),
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "Regex (content) or filename glob (files)"},
                "target": {"type": "string", "enum": ["content", "files"], "description": "Default content"},
                "path": {"type": "string", "description": f"Directory or file (default {projects})"},
                "file_glob": {"type": "string", "description": "content: only files matching, e.g. '*.py'"},
                "context": {"type": "integer", "description": "content: lines of context (0-5)"},
                "ignore_case": {"type": "boolean"},
                "fixed_strings": {"type": "boolean", "description": "Treat pattern as a literal string"},
                "hidden": {"type": "boolean", "description": "Include dotfiles/dot-dirs"},
                "limit": {"type": "integer", "description": "Max results (default 50, max 500)"},
            }, "required": ["pattern"]},
        })
    ctx.register_tool(
        check_fn=_configured, name="mac_process", toolset="mac", handler=mac_process, emoji="🍎",
        schema={
            "name": "mac_process",
            "description": (f"Long-running jobs on the user's {name} (dev servers, watchers, long builds/tests) in "
                            "detached tmux sessions. start runs command in cwd; log/status read output and exit "
                            "code; send types input (+Enter) into it; stop sends Ctrl-C then kills it; list shows "
                            "jobs. The user can watch with `tmux -L hermes attach -t hermes-<name>`."),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "status", "log", "send", "stop", "list"]},
                "name": {"type": "string", "description": "Job name, e.g. 'web-dev' (letters, digits, _ -)"},
                "command": {"type": "string", "description": "start: e.g. 'pnpm dev'"},
                "cwd": {"type": "string", "description": f"start: directory, e.g. {projects}/myrepo"},
                "input": {"type": "string", "description": "send: text to type"},
                "enter": {"type": "boolean", "description": "send: press Enter after input (default true)"},
                "lines": {"type": "integer", "description": "log: last N lines (default 100, max 2000)"},
            }, "required": ["action"]},
        })
    ctx.register_hook("pre_tool_call", _pre_tool_call)

    # mac_computer_use: the built-in computer_use vocabulary, driving the Mac (computer_use itself keeps driving
    # this machine's own screen). The module and its backend are loaded from this plugin's directory.
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_hermes_remote_hands_mac_cu", Path(__file__).with_name("mac_cu.py"))
    mac_cu = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(mac_cu)
    mac_cu.configure(settings_fn=_node)
    ctx.register_tool(name="mac_computer_use", toolset="mac", handler=mac_cu.make_handler(_hermes_home),
                      schema=mac_cu.schema(name), emoji="🍎", check_fn=_configured)
    with contextlib.suppress(Exception):  # disable / reload: close this load's SSH sessions to the Mac
        ctx.on_unload(mac_cu._release_all)
    ctx.register_hook("on_session_finalize", mac_cu.release)
    ctx.register_hook("on_session_reset", mac_cu.release)
    ctx.register_hook("post_compaction", mac_cu.on_compaction)
