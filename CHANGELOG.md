# Changelog

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
