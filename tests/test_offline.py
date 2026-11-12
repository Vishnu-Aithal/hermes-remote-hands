"""Offline tests: settings validation, the bypass guard, project-root limits, approval keys, driver-path checks.

No Mac, no network, no Hermes checkout needed (stdlib only; ssh lookups and remote path resolution are stubbed).

    python3 tests/test_offline.py        # or: python3 -m pytest tests/test_offline.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
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


def test_bypass_guard_messages():
    configure(**GOOD)
    mac = _hook("ssh macbook id")["message"]
    assert "reaching the user's Mac" in mac and "One-time approval" in mac, mac
    for cmd in ("git -C ~/.hermes/plugins/hermes-remote-hands rev-parse HEAD",
                "hermes config set plugins.entries.hermes-remote-hands.settings.name Mac"):
        own = _hook(cmd)["message"]
        assert "plugin's own code or settings" in own and "reaching" not in own and "One-time" in own, own
    both = _hook("scp ~/.hermes/plugins/hermes-remote-hands/x.py macbook:/tmp/")["message"]
    assert "reaching the user's Mac" in both, both  # the Mac message wins when both match
    own = m._pre_tool_call("search_files", {"pattern": "entries\\.hermes-remote-hands", "path": "."})
    assert own is not None and "own code or settings" in own["message"], own
    run = _hook("python3 ~/.hermes/plugins/hermes-remote-hands/tests/selftest_dev.py")["message"]
    assert "running the plugin's code connects to the Mac" in run, run  # never sounds local-only
    for cmd in ("cat ~/.hermes/plugins//hermes-remote-hands/x.py", "cat ~/.hermes/plugins/./hermes-remote-hands/x"):
        assert _hook(cmd) is not None, cmd


def test_bypass_guard_large_args_stay_fast():
    # The hook runs on every tool call while holding the GIL: a big argument full of `ssh` words must not make the
    # control-socket check quadratic. The old `\bssh\b.*(...)` regex took ~2 s on 40 KB of these lines and grew with
    # the square of the size. Indented: in the JSON blob a bare "\n" glues to the next word ("nssh"), so unindented
    # lines leave only one `ssh` word and never showed the slowdown.
    configure(**GOOD)
    content = "  ssh host x\n" * (200_000 // 13)
    for args, want in (({"path": "notes.txt", "content": content}, None),
                       ({"path": "notes.txt", "content": content + "ssh -S /tmp/sock h id\n"}, "ssh control socket"),
                       ({"command": "ssh " + "-o a=b " * 200 + "-S /tmp/sock h id"}, "ssh control socket")):
        t0 = time.perf_counter()
        res = m._pre_tool_call("write_file" if "content" in args else "terminal", args)
        took = time.perf_counter() - t0
        assert took < 1.0, f"guard took {took:.2f}s"
        assert (res is None) if want is None else (want in res["message"]), res
    assert m._control_socket_use("x ssh y -s ~/s") and not m._control_socket_use("-s ~/s then ssh y")


def test_bypass_guard_sees_line_breaks_as_separators():
    # In the JSON blob a newline/tab is the escape `\n`/`\t`; once backslashes are stripped it must not glue onto
    # the next word (`nssh`), or a command or alias at the start of a line slips past the guard.
    configure(**GOOD)
    ssh, sock = "s" + "sh", "-" + "S /tmp/s"
    for cmd in (f"cd /tmp\n{ssh} {sock} x id", f"cd /tmp\t{ssh} {sock} x id", f"{ssh}\t{sock} x id",
                f"{ssh} -{chr(83)}\n/tmp/s x id", "cat hosts\nmacbook\n", "a\r\nmacbook:/tmp"):
        assert _hook(cmd) is not None, repr(cmd)
    for cmd in ("printf 'a\\nb'", "echo \\\\n\\\\t", "ls\n\tgit status\r\n", "caf\u00e9 \u2028 notes"):
        assert _hook(cmd) is None, repr(cmd)
    # An escaped backslash before n/t stays a backslash (stripped like any other), not a line break.
    assert m._flatten(json.dumps("mac\\nbook")) == "macnbook" and m._flatten(json.dumps("a\nb")) == "a b"


def test_guard_fast_paths_match_reference_regexes():
    # The hook's string-scan helpers must keep the exact behaviour of the simple regexes they replace.
    import random
    import re
    rnd = random.Random(1)
    ssh, dash, cp = "s" + "sh", "-" + "s", "control" + "path"  # built here so this file itself stays unflagged
    toks = [ssh, " ", dash, "~", "/", "$", "${", "{", "}", "'", '"', "`", "\\", "\n", "\t", cp, "macbook", "mac",
            "book", "aa", "a", "_", "-", "0", "é", "@", ":", "."]
    old_socket = re.compile(r"\b" + ssh + r"\b.*(\s" + dash + r"(\s|[~/$])|" + cp + ")")
    for _ in range(20_000):
        t = "".join(rnd.choice(toks) for _ in range(rnd.randint(0, 12)))
        assert m._strip_quoting(t) == re.sub(r"[\\'\"`]|\$\{?\w*\}?", "", t), t
        assert m._control_socket_use(t) == (old_socket.search(t) is not None) or "\n" in t, t
        for mk in ("macbook", "aa", "a.a"):
            assert m._mentions(mk, t) == (re.search(r"(?<![a-z0-9])" + re.escape(mk) + r"(?![a-z0-9])", t)
                                          is not None), (mk, t)


def test_ssh_lookups_get_minimal_env():
    # `ssh -G` can run `Match exec` commands from ssh_config: it must never inherit provider keys.
    configure(**GOOD)
    seen = []

    def run(argv, **kw):
        seen.append(kw.get("env"))
        return _fake_run(argv, **kw)

    saved_key, orig = os.environ.get("OPENAI_API_KEY"), m.subprocess.run  # one shared subprocess module
    try:
        cu = _load("_rh_offline_cu_env", "mac_cu.py")
        cu.configure(settings_fn=m._node)
        rb = cu._remote_mac_module()
        os.environ["OPENAI_API_KEY"] = "sk-test-secret"
        m.subprocess.run = run
        assert m._env() == rb._ssh_env()  # the two modules' allow-lists must not drift apart
        m._ssh_target("macbook")
        m._identity_files("macbook")
        rb._target_cache.clear()
        rb._ssh_target("macbook")
    finally:
        m.subprocess.run = orig
        if saved_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = saved_key
    assert len(seen) == 3, seen
    for env in seen:
        assert env is not None and "OPENAI_API_KEY" not in env and "PATH" in env, env


def test_tools_need_home_too():
    configure(ssh_host="macbook")
    assert not m._configured()  # tools stay hidden until home is set as well
    configure(**GOOD)
    assert m._configured()


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
    _, d = cu._approval("click", "click", {}, "click element 3", "per_command", name="MacBook Pro")
    assert d == "On MacBook Pro: click element 3", d
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
