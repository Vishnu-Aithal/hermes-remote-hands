"""``mac_computer_use``: the built-in ``computer_use`` tool, pointed at the user's Mac.

The built-in ``computer_use`` keeps driving THIS machine's screen (the Bot Screen on the server). This tool reuses
the same action vocabulary, schema, hard blocks, element-token stale protection, screenshot dedup and dispatch
(``tools.computer_use.tool``), but with the ``RemoteMacBackend`` from ``remote_backend.py`` (cua-driver
over SSH), its own per-session backend cache, and its own approval keys (``mac_cua:*``), so a grant for the
server screen never covers the Mac and vice versa. The Bot Screen lease does not apply: the Mac is the user's own
desktop, not the server's.

Approval policy (stricter than the server screen, because this is the user's personal desktop):
  * observe (capture / list_apps / list_windows) — one grant per session (``mac_cua:observe``)
  * type / key — with ``shell_grants: per_command`` (default) a grant covers only that exact text / key combo,
    so approving one keystroke can't turn a focused Terminal into an unrestricted shell; the prompt shows the
    full text. ``per_session`` restores one grant per action.
  * other input actions — one grant per action + delivery mode, as in the built-in tool.
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import hashlib
import importlib.util
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("plugins.hermes_remote_hands.mac_cu")

_lock = threading.Lock()
_backends: dict[str, Any] = {}
_call_locks: dict[str, threading.RLock] = {}
_start_locks: dict[str, threading.Lock] = {}
_generation: dict[str, int] = {}   # bumped on release: a start that began before it must not be cached
_remote_mod = None

_OBSERVE = frozenset({"capture", "list_apps", "list_windows"})

# Hermes internals this tool relies on. Checked on every call so a Hermes update that renames one fails closed
# with a clear message instead of silently skipping a safety step.
_REQUIRED_TOOL_SYMBOLS = ("_reject_unsafe", "_ACTIONS", "_summarize_action", "_dispatch", "reset_screenshot_dedup",
                          "_scoped_sid")


def _tool_module():
    from tools.computer_use import tool as T
    missing = [s for s in _REQUIRED_TOOL_SYMBOLS if not hasattr(T, s)]
    if missing:
        raise RuntimeError(f"this Hermes version changed computer_use internals ({', '.join(missing)}); "
                           "update hermes-remote-hands and run its tests/selftest_screen.py")
    return T


_settings_fn = None  # the plugin's settings reader (hermes-remote-hands __init__._node), set by configure()


def configure(*, settings_fn) -> None:
    """Called once from register(): route node settings (ssh_host, driver, home, …) through the plugin config."""
    global _settings_fn
    _settings_fn = settings_fn
    if _remote_mod is not None:
        _remote_mod.configure(settings_fn=settings_fn)


def _remote_mac_module(hermes_home: Optional[Path] = None):
    """Load remote_backend.py (next to this file) under a private module name, once per process."""
    del hermes_home  # kept for call-site compatibility; the backend ships inside this plugin
    global _remote_mod
    with _lock:
        if _remote_mod is None:
            path = Path(__file__).with_name("remote_backend.py")
            spec = importlib.util.spec_from_file_location("_hermes_remote_hands_backend", path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"remote backend not found at {path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if _settings_fn is not None:
                mod.configure(settings_fn=_settings_fn)
            _remote_mod = mod
        return _remote_mod


def _key(session_id: str) -> str:
    """Cache key, profile-scoped exactly like the built-in tool's backend cache."""
    try:
        return _tool_module()._scoped_sid(session_id)
    except Exception:
        return str(session_id or "")


# ── backend lifecycle ───────────────────────────────────────────────────────

def _stop_async(backend: Any, lock: Optional[threading.RLock] = None, *, wait: bool = False) -> None:
    """Stop a backend off the caller's thread: stop() waits for an in-flight action and then talks to the Mac
    (up to ~30 s if it went to sleep), which must never stall session teardown in the gateway."""
    def _run():
        with contextlib.suppress(Exception), (lock or contextlib.nullcontext()):
            backend.stop()
    t = threading.Thread(target=_run, name="mac-cu-stop", daemon=True)
    t.start()
    if wait:
        t.join(timeout=10)


def _backend_for(key: str, hermes_home: Path) -> tuple[Any, threading.RLock]:
    """One backend per session. Start-up (SSH connect) runs under a PER-SESSION lock, never the global one.
    A failed start is torn down; a start overtaken by a release, or by a concurrent start, is discarded."""
    with _lock:
        if key in _backends:
            return _backends[key], _call_locks[key]
        start_lock = _start_locks.setdefault(key, threading.Lock())
        gen = _generation.get(key, 0)
    with start_lock:
        with _lock:
            if key in _backends:
                return _backends[key], _call_locks[key]
        backend = _remote_mac_module(hermes_home)._build_backend_cls()(permission_mode="standard")
        try:
            backend.start()  # raises a clear "Mac not reachable" error when asleep / off Tailscale
        except BaseException:
            _stop_async(backend)
            raise
        with _lock:
            if _generation.get(key, 0) != gen:
                discard, result = True, None           # the session ended while we were connecting
            elif key in _backends:
                discard, result = True, (_backends[key], _call_locks[key])  # a concurrent start won
            else:
                _backends[key], _call_locks[key] = backend, threading.RLock()
                return backend, _call_locks[key]
        if discard:
            _stop_async(backend)
        if result is None:
            raise RuntimeError("the session ended while connecting to the Mac")
        return result


def _release_key(key: str, *, wait: bool = False) -> None:
    with _lock:
        _generation[key] = _generation.get(key, 0) + 1
        backend, lock = _backends.pop(key, None), _call_locks.pop(key, None)
        sl = _start_locks.get(key)
        if sl is not None and not sl.locked():
            _start_locks.pop(key, None)
    with contextlib.suppress(Exception):
        _tool_module().reset_screenshot_dedup(_dedup_sid_for_key(key))
    if backend is not None:
        _stop_async(backend, lock, wait=wait)


def _dedup_sid(session_id: str) -> Optional[str]:
    return f"{session_id}#mac" if session_id else None


def _dedup_sid_for_key(key: str) -> str:
    return key.split("@", 1)[0] + "#mac"


def release(session_id: str = "", old_session_id: str = "", **_kw) -> None:
    """on_session_finalize / on_session_reset: drop the finished session's Mac connection (both ids on a reset)."""
    for sid in {str(session_id or ""), str(old_session_id or "")} - {""}:
        _release_key(_key(sid))


def on_compaction(session_id: str = "", old_session_id: str = "", **_kw) -> None:
    """post_compaction: (1) reset the Mac tool's screenshot dedup (core only resets the built-in tool's), so a
    "screen unchanged" note can't point at a dropped frame; (2) when compaction rotated the session id, carry the
    live Mac backend over to the new id instead of orphaning it."""
    with contextlib.suppress(Exception):
        T = _tool_module()
        for sid in {str(session_id or ""), str(old_session_id or "")} - {""}:
            T.reset_screenshot_dedup(f"{sid}#mac")
    old, new = str(old_session_id or ""), str(session_id or "")
    if not old or not new or old == new:
        return
    old_k, new_k = _key(old), _key(new)
    with _lock:
        if old_k in _backends and new_k not in _backends:
            _backends[new_k], _call_locks[new_k] = _backends.pop(old_k), _call_locks.pop(old_k)
            _generation[old_k] = _generation.get(old_k, 0) + 1
            return
    _release_key(old_k)


@atexit.register
def _release_all() -> None:
    for key in list(_backends):
        _release_key(key, wait=True)


# ── approvals ───────────────────────────────────────────────────────────────

def _shell_grants(hermes_home: Path) -> str:
    with contextlib.suppress(Exception):
        return str(_remote_mac_module(hermes_home).node_config().get("shell_grants") or "per_command")
    return "per_command"


# Input actions whose grant is keyed by the exact text they send (a per-action grant would let one approved
# keystroke into a focused Terminal become an open shell).
_PER_TEXT = {"type": "text", "key": "keys", "set_value": "value"}


def _approval(action: str, scope: str, args: dict[str, Any], summary: str, grants: str) -> tuple[str, str]:
    """(rule_key, description) for one approval scope."""
    mode = "foreground" if args.get("delivery_mode") == "foreground" else "background"
    if scope in _PER_TEXT and grants != "per_session":
        payload = str(args.get(_PER_TEXT[scope]) or "")
        digest = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:12]
        shown = json.dumps(payload[:2000]) + (" …(truncated)" if len(payload) > 2000 else "")
        return f"mac_cua:{scope}:{mode}:{digest}", f"On the user's Mac: {scope} {shown} ({mode})"
    return f"mac_cua:{scope}:{mode}", f"On the user's Mac: {summary}"


def _approve(rule_key: str, description: str, scope: str) -> Optional[str]:
    try:
        from tools.approval import request_tool_approval
        res = request_tool_approval("mac_computer_use", description, rule_key=rule_key)
    except Exception as e:  # fail closed
        return json.dumps({"error": f"approval gate unavailable ({e}); refusing to act on the Mac"})
    return None if res.get("approved") else json.dumps({"error": res.get("message") or "denied by user",
                                                        "action": scope})


# ── handler ─────────────────────────────────────────────────────────────────

def make_handler(hermes_home_fn):
    def mac_computer_use(args: dict[str, Any], session_id: str = "", **_kw) -> Any:
        try:
            T = _tool_module()
        except Exception as e:
            return json.dumps({"error": str(e)})
        action = (args.get("action") or "").strip().lower()
        if not action:
            return json.dumps({"error": "missing `action`"})
        home = hermes_home_fn()
        try:  # settings first: an unconfigured plugin must not ask the user to approve anything
            _remote_mac_module(home).node_config()
        except Exception as e:
            return json.dumps({"error": f"Mac computer use unavailable: {e}"})
        if (err := T._reject_unsafe(action, args)) is not None:
            return err
        scopes = (["observe"] if action in _OBSERVE else []) + (
            [action] if action in T._ACTIONS and T._ACTIONS[action].destructive else []) + (
            ["bring_to_front"] if args.get("bring_to_front") or (action == "focus_app" and args.get("raise_window"))
            else [])
        grants = _shell_grants(home) if action in _PER_TEXT else ""
        for scope in scopes:
            summary = "look at the screen (capture / list apps & windows)" if scope == "observe" \
                else T._summarize_action(scope, args)
            rule_key, description = _approval(action, scope, args, summary, grants)
            if (err := _approve(rule_key, description, scope)) is not None:
                return err
        sid = str(session_id or "")
        key = _key(sid)
        for _attempt in range(3):
            try:
                backend, call_lock = _backend_for(key, home)
            except Exception as e:
                return json.dumps({"error": f"Mac computer use unavailable: {e}"})
            try:
                with call_lock:
                    with _lock:  # re-validate: a release/compaction may have retired this backend while we waited
                        current = _backends.get(key) is backend and _call_locks.get(key) is call_lock
                    if not current:
                        continue
                    # Separate dedup namespace from the server screen's computer_use in the same session.
                    return T._dispatch(backend, action, args, session_id=_dedup_sid(sid))
            except Exception as e:
                logger.exception("mac_computer_use %s failed", action)
                return json.dumps({"error": f"{action} failed: {e}"})
        return json.dumps({"error": "the Mac session was released while this call waited; retry"})
    return mac_computer_use


def schema(name: str = "Mac") -> dict[str, Any]:
    from tools.computer_use.schema import COMPUTER_USE_SCHEMA
    s = copy.deepcopy(COMPUTER_USE_SCHEMA)
    s["name"] = "mac_computer_use"
    s["description"] = (
        f"Drive the screen of the USER'S OWN MAC ({name}, over SSH) — same actions and arguments as computer_use. "
        "Use this for anything in the user's Mac apps; `computer_use` drives the SERVER's own virtual screen "
        "instead. macOS shortcuts use cmd (cmd+s, cmd+t). Open apps with mac_shell(\"open -a <App>\") first, then "
        "capture(app=...). If the Mac is asleep or locked, captures fail: tell the user. Approvals here are "
        "stricter than on the server screen: looking at the screen needs one OK per session, and typed text / "
        "key combos / set_value are approved per exact text.\n\n" + s.get("description", ""))
    return s
