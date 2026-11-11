"""Self-test for the mac-shell dev tools, run against a throwaway dir on the Mac (<projects_dir>/hermes-devtest,
removed after; must be inside projects_dir so session grants apply).

    PY=$(find ~/.hermes/installs -path '*/venv/bin/python' | head -1); $PY -I tests/selftest_dev.py

Uses the plugin settings from config.yaml (plugins.entries.hermes-remote-hands.settings); projects_dir must
be set.

Approvals are stubbed with a recorder (approve + log rule_key/reason) so the gate DECISIONS are asserted too.
Never touches real sensitive files: the symlink target is a fake `.ssh` dir inside the test dir.
"""
import importlib.util, json, os, sys, time

sys.path.insert(0, os.environ.get("HERMES_AGENT_SRC") or os.path.expanduser("~/.hermes/hermes-agent"))
os.environ["HERMES_YOLO_MODE"] = "1"  # command guard: no human in this harness
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("remote_hands", os.path.join(PLUGIN_DIR, "__init__.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class _Ctx:  # minimal PluginContext stand-in: plugins.entries.<id>.settings.<key>, as ctx.get_config reads it
    plugin_id = "hermes-remote-hands"

    def get_config(self, key, default=None):
        from hermes_cli.config import load_config_readonly
        entries = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {})
        entry = entries.get(self.plugin_id) or {}
        return (entry.get("settings") or {}).get(key, default)


m._CTX = _Ctx()
PROJECTS = m._node().get("projects_dir") or sys.exit("set projects_dir first")

GATES: list[tuple[str, str, str]] = []
m._approve = lambda tool, key, reason: GATES.append((tool, key, reason)) or None


def call(fn, **p):
    t = time.monotonic()
    r = json.loads(getattr(m, fn)(p))
    print(f"### {fn} {p.get('action', '')} ({time.monotonic() - t:.2f}s) -> {json.dumps(r)[:400]}")
    return r


def last_gate():
    return GATES[-1] if GATES else ("", "", "")


D = f"{PROJECTS}/hermes-devtest"
ok = {}
call("mac_shell", command=f"rm -rf {PROJECTS}/hermes-devtest && mkdir -p {PROJECTS}/hermes-devtest/fake/.ssh && cd {PROJECTS}/hermes-devtest "
                          "&& git init -q && echo SECRETKEY > fake/.ssh/target && ln -s fake/.ssh/target link "
                          "&& ln -s fake/.ssh sshdir && ln -s fake/.zshenv dangling && mkdir -p .git/hooks "
                          "&& echo 'NPMTOKEN' > fake/.npmrc && echo ok")
code = "def add(a, b):\n    return a + b   \n\n\ndef sub(a, b):\n    return a - b\n\nprint(add(2, 3))\n"
call("mac_file", action="write", path=f"{D}/app.py", content=code)
r = call("mac_file", action="read", path=f"{D}/app.py", offset=4, limit=3)
ok["numbered_read"] = r.get("content", "").startswith("4|") and r.get("total_lines") == 8 and r.get("next_offset") == 7
ok["plain_path_session_grant"] = last_gate()[1] == "mac_file:read"
r = call("mac_patch", path=f"{D}/app.py", old_string="def add(a, b):\n    return a + b\n",
         new_string="def add(a, b):\n    return a + b + 0\n")
ok["patch_trailing_ws"] = bool(r.get("ok")) and "note" in r
r = call("mac_patch", path=f"{D}/app.py", old_string="(a, b)", new_string="(x, y)")
ok["patch_ambiguous_refused"] = "matches 2 places" in r.get("error", "")
r = call("mac_patch", path=f"{D}/app.py", old_string="print(add(2, 3))", new_string="print(add(2, 3)")
ok["patch_lint"] = bool(r.get("ok")) and "SyntaxError" in r.get("lint", "")
call("mac_patch", path=f"{D}/app.py", old_string="print(add(2, 3)", new_string="print(add(2, 3))")
# fallback match must edit the line that matched, not an earlier occurrence of its text
call("mac_file", action="write", path=f"{D}/ws.txt", content="xb = 1  \nb = 1  \n")
call("mac_patch", path=f"{D}/ws.txt", old_string="b = 1\n", new_string="b = 2\n")
r = call("mac_file", action="read", path=f"{D}/ws.txt")
ok["patch_fallback_position"] = r.get("content") == "xb = 1  \nb = 2\n"
call("mac_file", action="write", path=f"{D}/ws2.txt", content="k = 1 \nz\nk = 1\t\nz\n")
call("mac_patch", path=f"{D}/ws2.txt", old_string="k = 1\nz\n", new_string="k = 9\nz\n", replace_all=True)
r = call("mac_file", action="read", path=f"{D}/ws2.txt")
ok["patch_fallback_replace_all"] = r.get("content") == "k = 9\nz\nk = 9\nz\n"
# symlinks to sensitive places must lose the session grant
r = call("mac_file", action="read", path=f"{D}/link")
ok["symlink_file_flagged"] = "⚠" in last_gate()[2] and ":once:" in last_gate()[1] and "→" in last_gate()[2]
call("mac_patch", path=f"{D}/sshdir/target", old_string="SECRETKEY", new_string="X")
ok["symlink_dir_patch_flagged"] = "⚠" in last_gate()[2] and ":once:" in last_gate()[1]
r = call("mac_shell", command="python3 app.py && git status --short", cwd=D)
ok["shell_cwd"] = r.get("stdout", "").startswith("5") and "app.py" in r.get("stdout", "")
call("mac_shell", command="ls", cwd=f"{D}/sshdir")
ok["symlink_cwd_flagged"] = "SENSITIVE" in last_gate()[2] and ":once:" in last_gate()[1]
r = call("mac_search", pattern="return a", path=D)
ok["search_content"] = r.get("count") == 2
r = call("mac_search", pattern="SECRETKEY|X", path=D, hidden=True)
ok["search_excludes_ssh"] = "SECRETKEY" not in r.get("matches", "") and "fake/.ssh" not in r.get("matches", "")
r = call("mac_search", pattern="*.py", target="files", path=D)
ok["search_files"] = r.get("count") == 1
r = call("mac_search", pattern="=nomatch_zzz", path=D)
ok["search_nomatch_eq"] = r.get("count") == 0 and "error" not in r
# long-running jobs (isolated tmux server)
r = call("mac_process", action="start", name="devtest", cwd=D,
         command="echo starting; for i in 1 2 3; do echo tick $i; sleep 1; done; read -r ans; echo got:$ans; exit 7")
ok["proc_start"] = bool(r.get("ok"))
time.sleep(4)
r = call("mac_process", action="status", name="devtest")
ok["proc_running"] = bool(r.get("running")) and "tick 3" in r.get("output", "")
r = call("mac_shell", command="tmux -L hermes ls; echo ---; tmux ls 2>&1 | grep -c hermes-devtest || true")
ok["proc_isolated_server"] = "hermes-devtest" in r.get("stdout", "").split("---")[0] and \
    r.get("stdout", "").split("---")[1].strip() == "0"
r = call("mac_process", action="list")
ok["proc_list"] = "devtest" in r.get("running", [])
call("mac_process", action="send", name="devtest", input="hello")
time.sleep(2)
r = call("mac_process", action="log", name="devtest")
ok["proc_send_exit"] = (not r.get("running")) and "got:hello" in r.get("output", "") and r.get("exit_code") == 7
r = call("mac_process", action="start", name="a.b", command="true")
ok["proc_name_dot_refused"] = "name is required" in r.get("error", "")
call("mac_process", action="start", name="devsrv", cwd=D, command="python3 -m http.server 8765")
time.sleep(3)
r = call("mac_shell", command="curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/app.py")
ok["dev_server"] = r.get("stdout") == "200"
call("mac_process", action="stop", name="devsrv")
time.sleep(1)
r = call("mac_process", action="status", name="devsrv")
ok["proc_stop"] = not r.get("running")
# injection: path text is never interpreted
call("mac_file", action="read", path=f"{D}/$(touch /tmp/pwned)", offset=1)
r = call("mac_shell", command="ls /tmp/pwned 2>&1; true")
ok["no_injection"] = "No such file" in r.get("stdout", "")
# reviewer findings: dangling symlink, outside-roots writes, .git internals, hidden-search secrets
r = call("mac_file", action="write", path=f"{D}/dangling", content="x")
ok["dangling_symlink_flagged"] = ":once:" in last_gate()[1] and "fake/.zshenv" in last_gate()[2]
r = call("mac_shell", command="cat fake/.zshenv", cwd=D)
ok["write_went_to_resolved_target"] = r.get("stdout") == "x"
call("mac_file", action="write", path="~/hermes-outside-test.txt", content="x")
ok["outside_write_once"] = ":once:" in last_gate()[1] and "outside" in last_gate()[2]
call("mac_file", action="read", path="~/hermes-outside-test.txt")
ok["outside_read_separate_grant"] = last_gate()[1] == "mac_file:read:outside"
call("mac_file", action="write", path=f"{D}/.git/hooks/pre-commit", content="x")
ok["git_hooks_once"] = ":once:" in last_gate()[1] and ".git" in last_gate()[2]
ok["gitconfig_sensitive"] = bool(m._SENSITIVE_PATH_RE.search("/Users/u/.gitconfig")) and \
    bool(m._SENSITIVE_PATH_RE.search("/opt/homebrew/bin/git")) and bool(m._SENSITIVE_PATH_RE.search("/Users/u/.npmrc"))
r = call("mac_search", pattern="NPMTOKEN", path=D, hidden=True)
ok["search_excludes_npmrc"] = r.get("count") == 0
call("mac_file", action="write", path=f"{D}/long.js", content="a" * 5000 + "\nb\n")
r = call("mac_file", action="read", path=f"{D}/long.js", offset=1, limit=5)
ok["long_line_cut"] = "[line cut]" in r.get("content", "") and len(r.get("content", "")) < 2100
r = call("mac_shell", command="exit 142")
ok["exit_142_not_timeout"] = r.get("exit_code") == 142
r = call("mac_shell", command="sleep 6", timeout=2)
ok["real_timeout"] = "timed out" in r.get("error", "")
ok["locate_ff_not_newline"] = m._locate("a\x0cb  \nc\n", "a\nb") == ([], "none")
ok["clean_term"] = m._clean_term("\x1bP1;2qdata\x1b\\ \x1bMx 10%\r20%\n\x1b[31mred\x1b[0m") == "20%\nred"
hook = lambda c: m._pre_tool_call("terminal", {"command": c})
_h = m._node()["ssh_host"]
ok["hook_quoted_alias"] = (hook(f"ssh {_h[:2]}''{_h[2:]} id") is not None
                           and hook(f"ssh {_h[:2]}\\{_h[2:]} id") is not None)
ok["hook_control_socket"] = hook("ssh -S ~/.ssh/cm-h* x id") is not None
ok["hook_single_use"] = hook(f"ssh {m._node()['ssh_host']} id")["rule_key"] != hook(f"ssh {m._node()['ssh_host']} id")["rule_key"]
ok["hook_ignores_plain"] = hook("ls -la && git status") is None
# connection reuse
t = time.monotonic()
for _ in range(3):
    m._ssh("true", timeout=10)
ok["fast_reuse"] = (time.monotonic() - t) / 3 < 0.35
print(f"avg ssh call {(time.monotonic() - t) / 3:.3f}s")
call("mac_shell", command=f"rm -rf {PROJECTS}/hermes-devtest ~/hermes-outside-test.txt; rm -f ~/Library/Logs/hermes-dev/devtest.* "
                          "~/Library/Logs/hermes-dev/devsrv.*; echo cleaned")
print("\nRESULTS", json.dumps(ok, indent=1))
print("ALL OK" if all(ok.values()) else "FAILURES: " + ", ".join(k for k, v in ok.items() if not v))
sys.exit(0 if all(ok.values()) else 1)
