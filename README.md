# Hermes Remote Hands

**One Hermes brain on a server. Hands on your own Mac.**

Run Hermes Agent on an always-on server (a VPS, a home box, a cloud VM) and let it work on the Mac in front of you:
see and click its apps, run commands, edit code, run dev servers. Memory, skills, sessions and cron stay in one
place on the server. Nothing agent-like runs on the Mac: no second Hermes, no second memory, no model calls.

```
 ┌──────────────── server (the brain) ───────────────┐
 │ Hermes: memory · skills · sessions · cron          │
 │ server hands: terminal · browser · computer_use    │
 │ Mac hands:    mac_computer_use · mac_shell · …     │──┐
 └────────────────────────────────────────────────────┘  │ SSH over Tailscale
 ┌──────────────── your Mac (hands only) ────────────┐  │ (no public ports)
 │ sshd (key-only) · cua-driver (screen + input)      │◀─┘
 └────────────────────────────────────────────────────┘
```

People keep asking for this ([#103932](https://github.com/NousResearch/hermes-agent/issues/103932),
[#18715](https://github.com/NousResearch/hermes-agent/issues/18715)): a separate Hermes per machine splits your
memory, and the built-in `computer_use` only drives the machine Hermes itself runs on.

## Tools

| Tool | What it does on the Mac |
|---|---|
| `mac_computer_use` | Screen and input: the **same actions, element indices, hard blocks and verify/escalate ladder as Hermes's built-in `computer_use`**, but driving the Mac through [cua-driver](https://github.com/trycua/cua) in the background (your cursor and focus are left alone). |
| `mac_shell` | Run a command in your zsh login shell (optional `cwd`, remote-enforced timeout). |
| `mac_file` | Read (numbered, with offset/limit), write, append or list text files. |
| `mac_patch` | Exact find-and-replace edit with a unified diff; refuses if the file changed since it was read. |
| `mac_search` | ripgrep over your projects (content or file names), never descending into credential stores. |
| `mac_process` | Long jobs (dev servers, watchers, builds) in a private `tmux -L hermes` server; start / log / send / stop. |

The built-in `computer_use`, `terminal`, file and browser tools keep working on the **server**, so one chat can
use both: "check the deploy logs on the server, then reproduce the bug in my local dev server."

## How it works

- **Transport:** plain OpenSSH to an alias in `~/.ssh/config`, key-only (`BatchMode`, `StrictHostKeyChecking`),
  with connection reuse (~0.1 s per call). Tailscale (or any private network) keeps port 22 off the internet.
- **Screen:** `cua-driver mcp` is started over SSH. On macOS it proxies to the CuaDriver.app daemon in your
  logged-in session, so screenshots and input use the Mac's own Accessibility / Screen Recording grants. The
  plugin reuses Hermes's own `CuaDriverBackend` and `computer_use` dispatch with only the transport swapped. It
  also adapts element tokens and labels for cua-driver 0.4x, which the Hermes 0.23 wrapper predates.
- **Timeouts are real:** each remote command runs under a process-group reaper, so a timeout kills the command
  on the Mac, not just the local SSH client.

## Approvals (stricter than the server screen)

Every call goes through Hermes's own approval gate (`request_tool_approval`), with its own `mac_*` keys, so
granting something on the server never grants it on the Mac.

- **Commands and typed text** are approved **per exact command / text** by default (`shell_grants: per_command`).
  One approved keystroke into a focused Terminal does not become an open shell.
- **Sensitive commands and paths always prompt, every time:** `security`, `osascript`, `launchctl`, `sudo`,
  `curl`/`ssh`/`nc`, `defaults write`, …; `~/.ssh`, Keychains, browser profiles and cookies, `~/.aws`, shell rc and
  other startup files, LaunchAgents, `.git/` internals, anything outside your project folders. Paths are resolved
  **on the Mac** (symlinks included, even dangling ones) before the check.
- **Looking at the screen** (capture / list apps & windows) needs one OK per session.
- Hermes's terminal guards (hardline blocks, dangerous-pattern prompts) run on every `mac_shell` command.
- **Bypass guard:** a `pre_tool_call` hook sends any *other* tool call that mentions the Mac (its SSH alias or IP,
  its key file, the SSH control socket) to a single-use approval, so `terminal: ssh <mac> …` can't route around
  these rules.
- Unattended runs (cron, no human attached) fail closed.

## Requirements

- Hermes Agent **≥ 0.23.0** on the server (Linux or macOS), with OpenSSH.
- A Mac on macOS 14+ that the server can reach over SSH; [Tailscale](https://tailscale.com) is recommended.
- For `mac_computer_use`: [cua-driver](https://cua.ai/docs/cua-driver) on the Mac (tested: **0.44.0**).

## Setup

**1. On the server:** make a dedicated key and an SSH alias.

```bash
ssh-keygen -t ed25519 -N "" -C hermes-remote-hands -f ~/.ssh/hermes_remote_hands
cat >> ~/.ssh/config <<'EOF'
Host macbook
  HostName 100.x.y.z            # the Mac's Tailscale IP
  User you
  IdentityFile ~/.ssh/hermes_remote_hands
  IdentitiesOnly yes
EOF
ssh-keyscan 100.x.y.z >> ~/.ssh/known_hosts   # compare with the Mac's keys
```

**2. On the Mac:** System Settings → General → Sharing → **Remote Login** on. Then, in a text editor, add the
server's public key (`~/.ssh/hermes_remote_hands.pub` on the server) as one new line of the Mac's
`authorized_keys` file, prefixed with options that restrict it to the server's address and disable forwarding:

```text
restrict,from="<server tailscale ip>" ssh-ed25519 AAAA… hermes-remote-hands
```

Keep that file readable only by you (mode 600).

Recommended: turn off password logins.

```bash
sudo tee /etc/ssh/sshd_config.d/000-hermes-hardening.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sudo sshd -t && echo OK
```

**3. On the Mac, for screen control:** install cua-driver, grant permissions **at the Mac itself** (prompts don't
work over SSH), and let launchd own the daemon:

```bash
/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"
~/.local/bin/cua-driver permissions grant      # Accessibility + Screen Recording
~/.local/bin/cua-driver telemetry disable      # optional
~/.local/bin/cua-driver stop
cat > ~/Library/LaunchAgents/com.trycua.cua-driver.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.trycua.cua-driver</string>
  <key>ProgramArguments</key><array>
    <string>/Applications/CuaDriver.app/Contents/MacOS/cua-driver</string><string>serve</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>
</dict></plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trycua.cua-driver.plist
~/.local/bin/cua-driver permissions status --json   # accessibility/screen_recording true, "driver-daemon"
```

**4. On the server:** install, configure, restart.

```bash
hermes plugins install hermes-remote-hands --enable
hermes config set plugins.entries.hermes-remote-hands.settings.ssh_host macbook
hermes config set plugins.entries.hermes-remote-hands.settings.home /Users/you
hermes config set plugins.entries.hermes-remote-hands.settings.projects_dir ~/Documents/Code   # optional
```

Restart the gateway (or start a new CLI session); the `mac_*` tools appear in the `mac` toolset. The same settings
are on the plugin's page in Hermes Desktop (**Settings → Plugins**).

| Setting | Default | Meaning |
|---|---|---|
| `ssh_host` | — (required) | SSH alias from `~/.ssh/config`. |
| `home` | — (required) | Absolute home dir on the Mac (`~` expands to it). |
| `name` | `Mac` | How tools refer to the machine. |
| `driver` | `~/.local/bin/cua-driver` | cua-driver path on the Mac. |
| `projects_dir` | — | Where your code lives; session file grants reach only here (plus `write_roots`). |
| `write_roots` | `[]` | Extra folders session file grants may reach. |
| `shell_grants` | `per_command` | `per_session` = one grant per chat (an unrestricted shell). |

## Try it

> On my Mac, open Calculator and work out 12 × 12.

> On my Mac, in ~/Documents/Code/myapp, start the dev server, then tell me what the first page logs.

> Check the server's disk usage, then compare it with my Mac's.

## Disclosure

- **What it reaches:** the Mac you configure, over SSH with the key in your `~/.ssh/config`. It runs shell
  commands, reads and writes files, and takes screenshots and sends input there, only through the tools above
  and only after Hermes's approval gate. **Anyone who controls the Hermes server can control the Mac**: protect
  the server (and its web/desktop login) accordingly.
- **Credentials:** the plugin never reads, stores or copies keys. OpenSSH uses the key named in `~/.ssh/config`.
  Calls to the system `ssh -G` read that config to resolve the alias, which the security scan reports as
  `ssh_dir_access`.
- **Network:** SSH to the configured host only. No third-party services, no telemetry. cua-driver's own telemetry
  is disabled per call when Hermes's `computer_use.cua_telemetry` is off.
- **Background processes on the Mac:** `mac_process` jobs in a private tmux server (`tmux -L hermes`), logs in
  `~/Library/Logs/hermes-dev/`; an SSH ControlMaster on the server for up to 30 s after the last call.
- **Screenshots** of the Mac are handled like built-in `computer_use` captures (saved under
  `$HERMES_HOME/cache/images`, sent to your configured model or auxiliary vision model).
- **Internals:** to share Hermes's `computer_use` behaviour exactly, the plugin subclasses and calls
  `tools.computer_use` internals (no runtime patching). It checks for them at load and fails closed with a clear
  message if a Hermes release changes them; run the self-tests after updating.

## Limitations

- macOS only (zsh login shell, cua-driver's macOS daemon). The Mac must be awake; a locked screen gives empty
  captures (the tools say so), and screen control needs your user logged in at the console.
- No "user is typing" detection: input from you and the agent at the same moment can interleave.
- cua-driver versions other than 0.44.0 log a warning until verified with the self-test.

## Tests

Run on the server against a real Mac (awake, unlocked):

```bash
PY=$(find ~/.hermes/installs -path '*/venv/bin/python' | head -1)
$PY tests/selftest_screen.py     # opens Calculator, clicks 7 × 6 = by element index, reads 42 back, checks hard blocks
$PY -I tests/selftest_dev.py      # 41 checks: path gates, symlinks, approvals, bypass guard, timeouts, tmux, patch
```

The tests replace the approval gate with a recorder (or set YOLO for their own process), because no human is
attached. The plugin itself never does.

## License

MIT
