# Changelog

## 0.5.3 — 2026-11-12
- Bypass guard: the control-socket check (`ssh … -S <socket>` / `ControlPath`) is now linear time. The old
  `\bssh\b.*(…)` regex grew with the square of the number of `ssh` words, so one large argument (e.g. a
  200 KB `write_file` of indented `ssh host x` lines, ~45 s) froze the whole process while the hook ran. Same matches as before, with
  no length window that padding could get around. New offline test.
- Bypass guard: the marker match and quote/`$var` stripping use plain string scans instead of regexes that were
  tried at every position. Same results (fuzz-tested against the old regexes); a 2 MB argument now costs under
  1 s instead of up to 3.6 s.
- Bypass guard: line breaks, tabs and other escaped characters in the arguments now count as word separators. Before,
  the JSON escape `\n` glued onto the next word once backslashes were stripped (`\nssh` → `nssh`), so a
  control-socket `ssh` or the Mac's alias at the start of a line was missed. New offline test.
- `ssh -G` lookups (alias → IP, key names) now run with the same minimal environment as every other ssh child,
  so a `Match exec` in ssh_config never sees provider keys. New offline test.

## 0.5.2 — 2026-11-11
- Tools appear only once both `ssh_host` and `home` are set (as documented).
- Bypass guard: plain-language prompts; touching the plugin's own code now also says that running it connects to
  the Mac; `a//b` and `a/./b` spellings of the plugin path are caught too.
- Clearer errors: one "not reachable" wording (now mentions Remote Login), a woken Mac is retried after 3 s
  instead of 20 s, timeouts say whether the command was stopped and point to `mac_process`, cua-driver failures
  say what to check, a missing `python3` is named instead of "could not resolve symlinks" (and an unchecked
  folder no longer reads "SENSITIVE command"), install hints for `tmux`.
- Screen approval cards use the Mac's configured name; shorter tool descriptions.
- README: first-run check, example values for the placeholders, step-4 details, more troubleshooting rows,
  approvals-off/YOLO caveat, cua-driver installer not pinned, full background-process disclosure.

## 0.5.1 — 2026-11-11
- Bypass-guard prompts say what matched: a call that only touches this plugin's own code or settings on the
  server no longer claims it is "reaching the user's Mac". Every prompt now states it is a one-time approval
  (a 'session' or 'always' answer covers only that call).

## 0.5.0 — 2026-11-11
- Settings are type-checked and normalised (`ssh_host` charset, absolute `home`, `write_roots` list or comma text,
  `shell_grants` choices); the legacy `remote_hands.json` fallback is gone.
- Project folders can never be home itself or one of its parents (`~`, `/Users`, `~/..` are refused).
- The bypass guard stays on when settings are missing or broken, matches markers on word boundaries, and also
  guards this plugin's own settings and code.
- `set_value` is approved per exact value, like `type` and `key`.
- ssh argv ends options with `--`; the cua-driver path must be absolute; the control socket moved from `~/.ssh`
  to `$HERMES_HOME/cache/ssh` (mode 700). Failed `ssh -G` lookups are no longer cached.
- Tools stay hidden until configured; an unconfigured `mac_computer_use` errors before asking for approval.
- SSH sessions are closed on plugin unload. New offline test suite + CI.
- README: setup order fixed (host key after Remote Login), Mac prerequisites, full-disk-access note, quoted `~`,
  telemetry off for the daemon, comparison with other approaches, troubleshooting, uninstall.

## 0.4.0 — 2026-11-11
- First public release: `mac_computer_use`, `mac_shell`, `mac_file`, `mac_patch`, `mac_search`, `mac_process`.
