"""Offline tests: settings validation, the bypass guard, project-root limits, approval keys, driver-path checks.

No Mac, no network, no Hermes checkout needed (stdlib only; ssh lookups and remote path resolution are stubbed).

    python3 tests/test_offline.py        # or: python3 -m pytest tests/test_offline.py
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="rh-offline-")


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Ctx:
    def __init__(self, **settings):
        self.settings = settings

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class _Done:
    def __init__(self, stdout):
        self.stdout, self.returncode = stdout, 0


def _fake_run(argv, **_kw):  # `ssh -G -- macbook`
    if argv[:2] == ["ssh", "-G"] and argv[-1] == "macbook":
        return _Done("hostname 100.64.1.2\nport 22\nidentityfile ~/.ssh/hermes_remote_hands\n"
                     "identityfile ~/.ssh/id_ed25519\n")
    return _Done("")


m = _load("_rh_offline", "__init__.py")
m.subprocess.run = _fake_run
m._resolve = lambda p: p  # remote realpath: identity


def configure(**settings):
    m._CTX = Ctx(**settings)
    m._roots_cache.clear()
    m._target_cache.clear()
    m._key_cache.clear()


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    return False


GOOD = dict(ssh_host="macbook", home="/Users/u", projects_dir="~/Code")


def test_settings_validation():
    configure()
    assert raises(m._node, m.NotConfigured) and not m._configured()
    for bad in ("mac book", "-oProxyCommand=x", "a;b", "a\nb"):
        configure(ssh_host=bad, home="/Users/u")
        assert raises(m._node), bad
    configure(ssh_host="macbook", home=["/Users/u"])
    assert raises(m._node)
    configure(ssh_host="macbook", home="relative/home")
    assert raises(m._node)
    configure(ssh_host="macbook", home="/Users//u/.", write_roots="~/a, ~/b", shell_grants="PER_SESSION")
    n = m._node()
    assert n["home"] == "/Users/u" and n["write_roots"] == ["~/a", "~/b"] and n["shell_grants"] == "per_command"
    configure(ssh_host=1234, home="/Users/u", write_roots=["~/a", 5, {"x": 1}])
    n = m._node()
    assert n["ssh_host"] == "1234" and n["write_roots"] == ["~/a"]
    configure(**GOOD)
    assert m._configured()


def test_project_roots_never_cover_home():
    configure(ssh_host="macbook", home="/Users/u", projects_dir="~/Code",
              write_roots=["~", "/Users", "~/..", "/", "/Users/u/", "~/Sites"])
    assert m._roots() == ("/Users/u/Code", "/Users/u/Sites"), m._roots()


def _hook(cmd):
    return m._pre_tool_call("terminal", {"command": cmd})


def test_bypass_guard_configured():
    configure(**GOOD)
    for cmd in ("ssh macbook id", "ssh mac''book id", "ssh mac\\book id", "ssh you@macbook:22 id",
                "ssh 100.64.1.2 id", "cat ~/.ssh/hermes_remote_hands", "scp x macbook:/tmp/",
                "ssh -S /tmp/sock x id", "ssh -o ControlPath=/x/y anyhost id"):
        assert _hook(cmd) is not None, cmd
    for cmd in ("ls -la && git status", "echo umbrella", "ssh other-server du -sh /var", "cat ~/.ssh/id_ed25519.pub",
                "macbookpro-notes.txt"):
        assert _hook(cmd) is None, cmd
    a, b = _hook("ssh macbook id"), _hook("ssh macbook id")
    assert a["action"] == "approve" and a["rule_key"] != b["rule_key"]  # single-use
    assert m._pre_tool_call("mac_shell", {"command": "ssh macbook"}) is None  # own tools gate themselves


def test_bypass_guard_survives_broken_settings():
    configure()  # unconfigured: static markers still guarded
    assert _hook("ssh -S ~/.hermes/cache/ssh/cm-hermes-abc x id") is not None
    assert _hook("hermes config set plugins.entries.hermes-remote-hands.settings.write_roots /") is not None
    assert _hook("ls") is None
    configure(ssh_host="macbook ", home=5)  # invalid settings: fall back to the raw alias
    assert _hook("ssh macbook id") is not None


def test_ssh_argv_has_end_of_options():
    configure(**GOOD)
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    orig = m.subprocess.run
    m.subprocess.run = run
    try:
        m._ssh("true", timeout=5)
    finally:
        m.subprocess.run = orig
    argv = seen["argv"]
    assert argv[argv.index("--") + 1] == "macbook"
    assert not any(".ssh/cm-" in a for a in argv)  # control socket lives under $HERMES_HOME, not ~/.ssh


def test_screen_backend_settings_and_grants():
    configure(**GOOD)
    cu = _load("_rh_offline_cu", "mac_cu.py")
    cu.configure(settings_fn=m._node)
    rb = cu._remote_mac_module()
    assert rb.node_config()["driver"] == "/Users/u/.local/bin/cua-driver"
    for bad in ("-oProxyCommand=/tmp/x", "=cmd", "relative/cua-driver"):
        configure(**GOOD, driver=bad)
        assert raises(rb.node_config), bad
    configure(**GOOD)
    k1, _ = cu._approval("set_value", "set_value", {"value": "rm -rf ~"}, "", "per_command")
    k2, _ = cu._approval("set_value", "set_value", {"value": "ls"}, "", "per_command")
    k3, _ = cu._approval("type", "type", {"text": "ls"}, "", "per_session")
    assert k1 != k2 and k1.startswith("mac_cua:set_value:") and k3 == "mac_cua:type:background"
    configure()
    handler = cu.make_handler(lambda: Path(os.environ["HERMES_HOME"]))
    err = json.loads(handler({"action": "capture"}))["error"]  # no approval prompt when unconfigured
    assert "not configured" in err, err


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"ALL OK ({len(tests)} tests)")
