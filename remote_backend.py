"""Remote cua-driver backend for ``mac_computer_use``: Hermes's own cua-driver backend, with the driver on a remote Mac.

The stock ``CuaDriverBackend`` spawns ``cua-driver mcp`` as a local stdio child. Here the child is
``ssh <host> <driver> mcp --no-overlay``; on macOS that process proxies to the CuaDriver.app daemon in the
logged-in GUI session, so screenshots and input keep the Mac's own TCC grants (Accessibility, Screen
Recording). Everything above the transport — the computer_use action vocabulary, element indices with
stale-token protection, approvals, hard blocks, screenshot dedup, the verify/escalate ladder — is the
built-in code, unchanged.

Node settings come from the plugin config (``ssh_host``, ``driver``, ``home``). Verify after any Hermes or
cua-driver update with ``tests/selftest_screen.py``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("plugins.hermes_remote_hands.backend")

# cua-driver versions this adapter has been verified against end to end (tests/selftest_screen.py: capture -> click by index
# -> read back). Hermes itself pins 0.21.x for LOCAL use; the Mac runs cua's own installer build. Others warn.
TESTED_DRIVER_VERSIONS = ("0.44.0",)

_SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]

# Hermes internals this plugin subclasses or calls. Checked before use so a Hermes update that renames one
# fails loudly with a clear message instead of misbehaving.
_REQUIRED_INTERNALS = {
    "tools.computer_use.cua_backend": ("CuaDriverBackend", "_computer_use_max_image_dimension", "_cua_no_overlay",
                                       "_cua_telemetry_disabled", "_CUA_TELEMETRY_ENV_VAR"),
    "tools.computer_use.cua_backend_session": ("_CuaDriverSession", "_AsyncBridge", "_cli_result", "_cli_run_json"),
}
_REQUIRED_METHODS = {
    "_CuaDriverSession": ("_lifecycle_coro", "_populate_capabilities", "_call_tool_via_cli", "call_tool",
                          "set_transport_reset_callback", "supports_input_property"),
    "CuaDriverBackend": ("_capture_window_state", "_handle_transport_reset", "_best_effort", "set_config",
                         "set_agent_cursor_enabled"),
}


# ── node config + reachability ──────────────────────────────────────────────

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


_settings_fn = None  # set by configure(): the plugin's settings reader (validates ssh_host)


def configure(*, settings_fn) -> None:
    global _settings_fn
    _settings_fn = settings_fn


def node_config() -> dict[str, Any]:
    """Node settings from the plugin config (``configure``), or the legacy ``$HERMES_HOME/remote_hands.json``."""
    if _settings_fn is not None:
        cfg = dict(_settings_fn())
    else:
        cfg = {"name": "Mac", "ssh_host": "", "driver": "~/.local/bin/cua-driver", "home": ""}
        with contextlib.suppress(Exception):
            cfg.update(json.loads((_hermes_home() / "remote_hands.json").read_text(encoding="utf-8")))
    host = str(cfg.get("ssh_host") or "")
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ValueError(f"invalid or missing ssh_host {host!r}")  # never let config smuggle ssh options
    # The driver path is single-quoted on the remote command line, so a leading ~ must be expanded here.
    driver, home = str(cfg.get("driver") or "~/.local/bin/cua-driver"), str(cfg.get("home") or "").rstrip("/")
    if driver.startswith("~/"):
        if not home.startswith("/"):
            raise ValueError("set the absolute 'home' setting (e.g. /Users/you) so '~' in the driver path resolves")
        driver = home + driver[1:]
    cfg["driver"] = driver
    return cfg


_reach_lock = threading.Lock()
_reach_cache: dict[str, tuple[float, bool]] = {}
_target_cache: dict[tuple[str, float], tuple[str, int]] = {}
_REACH_TTL = 20.0


def _ssh_config_mtime() -> float:
    try:
        return (Path.home() / ".ssh" / "config").stat().st_mtime
    except OSError:
        return 0.0


def _ssh_target(host: str) -> tuple[str, int]:
    """(hostname, port) as ssh itself resolves the alias (ssh -G reads ~/.ssh/config); cached until that file
    changes (e.g. the Mac's Tailscale IP moves)."""
    key = (host, _ssh_config_mtime())
    if key not in _target_cache:
        hostname, port = host, 22
        with contextlib.suppress(Exception):
            out = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=5,
                                 stdin=subprocess.DEVNULL).stdout
            kv = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
            hostname, port = kv.get("hostname", host), int(kv.get("port", "22"))
        _target_cache[key] = (hostname, port)
    return _target_cache[key]


def node_reachable(host: Optional[str] = None) -> bool:
    """TCP probe of the node's SSH port, cached 20 s. Called at session start, not in the tool-list hot path."""
    try:
        host = host or node_config()["ssh_host"]
        with _reach_lock:
            hit = _reach_cache.get(host)
            if hit and time.monotonic() - hit[0] < _REACH_TTL:
                return hit[1]
        ok = False
        with contextlib.suppress(OSError):
            with socket.create_connection(_ssh_target(host), timeout=2.0):
                ok = True
        with _reach_lock:
            _reach_cache[host] = (time.monotonic(), ok)
        return ok
    except Exception:
        return False


def _check_internals() -> None:
    import importlib
    missing = []
    mods = {name: importlib.import_module(name) for name in _REQUIRED_INTERNALS}
    for name, attrs in _REQUIRED_INTERNALS.items():
        missing += [f"{name}.{a}" for a in attrs if not hasattr(mods[name], a)]
    classes = {"_CuaDriverSession": getattr(mods["tools.computer_use.cua_backend_session"], "_CuaDriverSession", None),
               "CuaDriverBackend": getattr(mods["tools.computer_use.cua_backend"], "CuaDriverBackend", None)}
    for cls_name, methods in _REQUIRED_METHODS.items():
        cls = classes[cls_name]
        missing += [f"{cls_name}.{m}" for m in methods if cls is None or not hasattr(cls, m)]
    if missing:
        raise RuntimeError("hermes-remote-hands: this Hermes version changed computer-use internals the plugin relies on "
                           f"({', '.join(missing)}). Update the plugin, then run its tests/selftest_screen.py.")


# ── token / label adapter for cua-driver 0.4x snapshots ─────────────────────

# Header rule the driver prints OUTSIDE the tree: "element_token for row [N] = s0000000a:N"
_TOKEN_RULE_RE = re.compile(r"^element_token for row \[N\] = (s[0-9a-f]+):N\b", re.MULTILINE)
_SNAPSHOT_ID_RE = re.compile(r"^s[0-9a-f]+$")
# One tree row: "- [5] AXButton (7) [id=Seven …]", '[3] AXTextField = "x"', '[2] AXLink "Docs"', "[9] AXButton [id=Save"
_ROW_RE = re.compile(r'^\s*(?:-\s+)?\[(\d+)\]\s+\w+'
                     r'(?:\s*=\s*"([^"]*)"|\s+"([^"]*)"|\s+\(([^)]*)\))?'
                     r'(?:\s+\[id=([^\s\]]+))?', re.MULTILINE)


def _snapshot_prefix(out: dict[str, Any]) -> Optional[str]:
    """Token prefix for THIS get_window_state result, or None (then no tokens are minted and element clicks
    fail closed with a driver error). The structured ``snapshot_id`` is the authority; the header rule, if
    present, must agree with it. Tree text (on-screen content) is never consulted, so a page can't forge it."""
    sc = out.get("structuredContent") if isinstance(out, dict) else None
    sid = str((sc or {}).get("snapshot_id") or "") if isinstance(sc, dict) else ""
    if not _SNAPSHOT_ID_RE.match(sid):
        return None
    data = out.get("data")
    header = data.split("\n- ", 1)[0] if isinstance(data, str) else ""   # text before the tree begins
    rules = set(_TOKEN_RULE_RE.findall(header))
    if rules and rules != {sid}:
        logger.warning("hermes-remote-hands: snapshot_id %s disagrees with token rule %s; not minting tokens", sid, rules)
        return None
    return f"{sid}:"


def _row_labels(tree: str) -> dict[int, str]:
    """Label per row index, first occurrence wins. Precedence: value / quoted / text in parens, then a
    digit-only paren (0.4x prints Calculator digits as ``(7)``) shown with its id when there is one
    (``7 (Seven)``) so a digit that is really an order number can't silently mislabel, then the bare id."""
    labels: dict[int, str] = {}
    for m in _ROW_RE.finditer(tree):
        idx = int(m.group(1))
        if idx in labels:
            continue
        value, quoted, paren, ident = m.group(2), m.group(3), m.group(4), m.group(5)
        if value or quoted:
            labels[idx] = value or quoted
        elif paren and not paren.strip().isdigit():
            labels[idx] = paren
        elif paren:
            labels[idx] = f"{paren.strip()} ({ident})" if ident else paren.strip()
        elif ident:
            labels[idx] = ident
    return labels


# ── transport: cua-driver over SSH ──────────────────────────────────────────

def _build_session_cls():
    from tools.computer_use import cua_backend as _cb
    from tools.computer_use.cua_backend_session import _CuaDriverSession, _cli_result, _cli_run_json

    class RemoteCuaDriverSession(_CuaDriverSession):
        """``_CuaDriverSession`` whose stdio MCP child is ``ssh <host> cua-driver mcp``."""

        def __init__(self, bridge: Any, node: dict[str, Any]) -> None:
            super().__init__(bridge, None)
            self._node = node
            self.last_window_state: Optional[dict[str, Any]] = None

        def _ssh_argv(self, remote_cmd: str) -> list[str]:
            return ["ssh", *_SSH_OPTS, self._node["ssh_host"], remote_cmd]

        def _driver_cmd(self, *args: str) -> str:
            """Remote command line, every element quoted. Telemetry follows the user's Hermes setting
            (``computer_use.cua_telemetry``), as Hermes does for a local driver."""
            env = f"env {_cb._CUA_TELEMETRY_ENV_VAR}=0 " if _cb._cua_telemetry_disabled() else ""
            return env + " ".join(shlex.quote(a) for a in (self._node["driver"], *args))

        @staticmethod
        def _child_env() -> dict[str, str]:
            # ssh only needs PATH/HOME (+ the agent socket if one exists); never hand it provider secrets.
            return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "SSH_AUTH_SOCK")}

        async def _lifecycle_coro(self) -> None:
            """Mirror of the base lifecycle (phases, ready/setup-error signalling, re-entrant teardown) with the
            child swapped for ssh. No local binary check or manifest discovery: the driver lives on the Mac."""
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            self._shutdown_event = asyncio.Event()
            t0 = time.monotonic()
            self._startup_phase = "ssh-connect"
            try:
                argv = self._ssh_argv(self._driver_cmd("mcp", "--no-overlay"))
                params = StdioServerParameters(command=argv[0], args=argv[1:], env=self._child_env())
                async with stdio_client(params) as (read, write):
                    self._startup_phase = "mcp-initialize"
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._startup_phase = "capability-discovery"
                        await self._populate_capabilities(session)
                        self._session, self._startup_phase = session, "ready"
                        self._ready_event.set()
                        logger.info("remote cua-driver session on %s ready in %.1fs",
                                    self._node["ssh_host"], time.monotonic() - t0)
                        await self._shutdown_event.wait()
            except BaseException as e:
                self._setup_error = e
                self._ready_event.set()
                raise
            finally:
                self._session, self._started = None, False

        def call_tool(self, name: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
            # cua-driver 0.4x addresses elements by `element_token` only and rejects the `element_index` Hermes
            # sends alongside it ("unknown argument"). Drop it only when the live schema doesn't accept it AND
            # the token is present — the token alone is the exact, stale-checked handle.
            props = (self._tool_schemas.get(name) or {}).get("properties") or {}
            if props and "element_index" in args and "element_index" not in props and args.get("element_token"):
                args = {k: v for k, v in args.items() if k != "element_index"}
            result = super().call_tool(name, args, timeout=timeout)
            if name == "get_window_state":
                self.last_window_state = result
            return result

        def _call_tool_via_cli(self, name: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
            """CLI fallback (the base uses it when the MCP socket hits EAGAIN on heavy calls): ``cua-driver call``
            over SSH. A ``get_window_state`` PNG is written to the user's private cache dir on the Mac (not the
            shared /tmp), pulled back over SSH, and always deleted remotely."""
            call_args, remote_shot, local_shot = dict(args), None, None
            if name == "get_window_state" and "screenshot_out_file" not in call_args:
                cache = (str(self._node.get("home") or "").rstrip("/") + "/Library/Caches") \
                    if str(self._node.get("home") or "").startswith("/") else "/tmp"
                remote_shot = f"{cache}/hermes_cua_shot_{uuid.uuid4().hex[:12]}.png"
                call_args["screenshot_out_file"] = remote_shot
            try:
                parsed = _cli_run_json(self._ssh_argv(self._driver_cmd("call", name, json.dumps(call_args))),
                                       self._child_env(), name, timeout)
                if remote_shot and isinstance(parsed, dict):
                    parsed.pop("screenshot_file_path", None)  # a Mac path; never stat it on the server
                    with contextlib.suppress(subprocess.TimeoutExpired, OSError, binascii.Error, ValueError):
                        pulled = subprocess.run(self._ssh_argv(f"base64 < {shlex.quote(remote_shot)}"),
                                                capture_output=True, text=True, timeout=timeout,
                                                stdin=subprocess.DEVNULL, env=self._child_env())
                        if pulled.returncode == 0 and pulled.stdout.strip():
                            png = base64.b64decode("".join(pulled.stdout.split()))
                            fd, local_shot = tempfile.mkstemp(prefix="cua_shot_", suffix=".png")
                            with os.fdopen(fd, "wb") as fh:
                                fh.write(png)
                            parsed["screenshot_file_path"] = local_shot
                result = _cli_result(parsed, local_shot)
                if name == "get_window_state":
                    self.last_window_state = result
                return result
            finally:
                if remote_shot:
                    with contextlib.suppress(Exception):
                        subprocess.run(self._ssh_argv(f"rm -f -- {shlex.quote(remote_shot)}"), timeout=15,
                                       capture_output=True, stdin=subprocess.DEVNULL, env=self._child_env())
                if local_shot and os.path.exists(local_shot):
                    with contextlib.suppress(OSError):
                        os.remove(local_shot)

    return RemoteCuaDriverSession


def _build_backend_cls():
    _check_internals()
    from tools.computer_use import cua_backend as _cb
    from tools.computer_use.cua_backend import CuaDriverBackend
    from tools.computer_use.cua_backend_session import _AsyncBridge

    session_cls = _build_session_cls()

    class RemoteMacBackend(CuaDriverBackend):
        """The stock backend with its transport swapped for SSH. ``permission_mode`` other than ``standard``
        would need a private daemon spawned on the Mac; the remote LaunchAgent daemon runs ``standard``,
        so Hermes-side approvals (and their yolo bypass) remain the gate."""

        def __init__(self, permission_mode: str = "standard") -> None:
            super().__init__("standard")
            if permission_mode != "standard":
                logger.warning("hermes-remote-hands: permission_mode %r requested; the Mac's daemon runs 'standard' "
                               "(Hermes approvals still apply)", permission_mode)
            self.requested_permission_mode = permission_mode
            self.remote_driver_version = ""
            self._node = node_config()
            self._bridge = _AsyncBridge()
            self._session = session_cls(self._bridge, self._node)
            self._session.set_transport_reset_callback(self._handle_transport_reset)

        def start(self) -> None:
            """``CuaDriverBackend.start`` minus the local binary/runtime-contract checks (the driver lives on the
            Mac) plus a reachability check and a remote version check."""
            from pm import ensure_import
            ensure_import("computer-use")
            import importlib
            importlib.invalidate_caches()
            if not node_reachable(self._node["ssh_host"]):
                raise RuntimeError(f"{self._node.get('name', 'the Mac')} ({self._node['ssh_host']}) is not reachable "
                                   "over SSH — it is asleep, the lid is closed, or it is off Tailscale. Ask the "
                                   "user to wake it, then retry.")
            self._session.start()
            self._best_effort("start_session failed (continuing anonymous)",
                              self._session.call_tool, "start_session", {"session": self._session_id})
            if self._session._started:
                max_dim = _cb._computer_use_max_image_dimension()
                if max_dim:
                    self._best_effort("set_config(max_image_dimension) failed",
                                      self.set_config, max_image_dimension=max_dim)
                if _cb._cua_no_overlay():  # same belt-and-suspenders as the base: the daemon may ignore --no-overlay
                    self._best_effort("set_agent_cursor_enabled failed",
                                      self.set_agent_cursor_enabled, False, cursor_id=self._session_id)
                self._check_remote_version()

        def _check_remote_version(self) -> None:
            """Record the Mac's driver version; warn when this adapter wasn't verified against it."""
            with contextlib.suppress(Exception):
                out = self._session.call_tool("get_config", {}, timeout=10.0)
                sc = out.get("structuredContent") if isinstance(out, dict) else None
                data = out.get("data") if isinstance(out, dict) else None
                blob = sc if isinstance(sc, dict) else (json.loads(data) if isinstance(data, str) else data) or {}
                version = str((blob.get("config") or blob).get("version") or "")
                self.remote_driver_version = version
                if version and version not in TESTED_DRIVER_VERSIONS:
                    logger.warning("hermes-remote-hands: the Mac runs cua-driver %s; this adapter is verified on %s. Run "
                                   "the plugin's tests/selftest_screen.py before trusting element clicks.",
                                   version, ", ".join(TESTED_DRIVER_VERSIONS))

        def is_available(self) -> bool:
            return True  # reachability is checked in start(), with a clear error, not in the tool-list hot path

        def _capture_window_state(self):
            """Stock capture, then adapt to cua-driver 0.4x snapshots: they carry no structured ``elements`` list
            (so the stock parser finds no tokens and drops digit labels like ``(7)``). Tokens are minted from the
            result's own ``snapshot_id`` (``<snapshot_id>:N``, the driver's declared rule)."""
            # Clear first, so a previous snapshot can never be used to mint tokens for this one.
            self._session.last_window_state = None
            png, mime, elements, title = super()._capture_window_state()
            if self._snapshot_tokens or not elements:
                return png, mime, elements, title
            out = self._session.last_window_state or {}
            prefix = _snapshot_prefix(out)
            if not prefix:
                return png, mime, elements, title
            sc = out.get("structuredContent") if isinstance(out.get("structuredContent"), dict) else {}
            tree = sc.get("tree_markdown") if isinstance(sc.get("tree_markdown"), str) else (
                out.get("data") if isinstance(out.get("data"), str) else "")
            labels = _row_labels(tree)
            for e in elements:
                e.element_token = f"{prefix}{e.index}"
                if not e.label and labels.get(e.index):
                    e.label = labels[e.index]
            self._snapshot_tokens = {e.index: e.element_token for e in elements}
            return png, mime, elements, title

    return RemoteMacBackend
