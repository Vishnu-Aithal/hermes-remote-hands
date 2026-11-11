"""End-to-end self-test for ``mac_computer_use`` (the production Mac-screen path). Run after any Hermes,
cua-driver or plugin update; the Mac must be awake, unlocked and logged in as the configured user.

    PY=$(find ~/.hermes/installs -path '*/venv/bin/python' | head -1); $PY tests/selftest_screen.py

Opens Calculator, clicks AC 7 x 6 = by element index through the real handler, reads 42 back from the
accessibility tree, checks a hard-blocked key combo, then releases the session. Approvals are bypassed for this
process only (no human is attached to answer them).
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
os.environ["HERMES_YOLO_MODE"] = "1"  # this process only

HOME = Path(os.path.expanduser("~/.hermes"))
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:  # minimal PluginContext stand-in: read settings the way the plugin does at runtime
    plugin_id = "hermes-remote-hands"

    def get_config(self, key, default=None):
        from hermes_cli.config import load_config_readonly
        entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get(self.plugin_id) or {}
        return (entry.get("settings") or {}).get(key, default)


plugin = _load("_selftest_remote_hands", "__init__.py")
plugin._CTX = _Ctx()
m = _load("_selftest_mac_cu", "mac_cu.py")
m.configure(settings_fn=plugin._node)
handler = m.make_handler(lambda: HOME)
host = m._remote_mac_module(HOME).node_config()["ssh_host"]
SID = "selftest-mac-cu"


def fail(msg):
    print("FAIL:", msg)
    subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", host, "osascript -e 'quit app \"Calculator\"'"], timeout=30)
    m._release_all()
    sys.exit(1)


def cu(**args):
    r = handler(args, session_id=SID)
    return r.get("text_summary") if isinstance(r, dict) else r


subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", host, "open -a Calculator && sleep 1.5"], timeout=30)
try:
    elements = json.loads(cu(action="capture", mode="ax", app="Calculator"))["elements"]
except Exception as e:
    fail(f"capture failed (Mac asleep, locked, or another user at the console?): {e}")
idx = {}
for e in elements:
    label = str(e.get("label") or "")
    idx.setdefault(label, e["index"])
    idx.setdefault(label.split(" (")[0], e["index"])
for label in ("All Clear", "7", "Multiply", "6", "Equals"):
    if label not in idx:
        fail(f"label {label!r} missing from capture")
    out = cu(action="click", element=idx[label])
    if '"ok": true' not in out:
        fail(f"click {label}: {out[:300]}")
time.sleep(0.5)
cu(action="capture", mode="ax", app="Calculator")
state = json.dumps(m._backends[SID]._session.last_window_state, ensure_ascii=False)
shown = re.findall(r'AXStaticText = \\"\W*([0-9.,]+)\\"', state)
blocked = handler({"action": "key", "keys": "cmd+ctrl+q"}, session_id=SID)
subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", host, "osascript -e 'quit app \"Calculator\"'"], timeout=30)
m.release(session_id=SID)
if "42" not in shown:
    fail(f"display reads {shown or 'nothing'}, expected 42")
if "blocked key combo" not in blocked:
    fail(f"lock-screen combo was not blocked: {blocked[:200]}")
if m._backends:
    fail("session not released")
print("PASS: mac_computer_use capture, element clicks, hard blocks and release work (7 x 6 = 42)")
