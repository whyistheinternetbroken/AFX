# Changelog

All notable changes to `reinit_afx_v2.py` are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project versioning follows the script's internal `v1`/`v2`/`v2a`/...
revision labels rather than strict [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **Per-node join timing breakdown (mode 3).** The session summary now
  splits the old `Parallel Peer Auto-Add (mode 3)` phase into two phases —
  `Parallel Peer Option 4 (mode 3)` (parallel LOADER/format/option-4 work
  up to the join barrier) and `Node join total` (the sequential join phase) —
  and emits one indented sub-row per peer:
  `- Node [<name> - <BMC IP>]   <secs>s (<mins>m)`. Node name is taken from
  the matching `secondary_nodes[].name` entry in the config (falls back to
  just the BMC IP when not present).
- `SessionLogger.record_phase(name, elapsed, outcome=, note=)` — records a
  phase with a precomputed elapsed time (used when the real boundaries are
  determined inside worker threads).
- `SessionLogger.add_phase_subtiming(phase, label, elapsed)` — attaches an
  indented timing row that is rendered under a parent phase in both the
  full session log and the standalone summary file. Thread-safe.
- `from __future__ import annotations` at the top of the script (PEP 563).
  Enables type hints without runtime cost or forward-reference issues.
- Type hints on a handful of public-ish helpers: `load_config_file`,
  `_ssh_connect_with_retry`, `_node_cfg_for`, `_is_valid_ipv4`,
  `_first_ipv4_in`, `_config_primary_node`, `_config_secondary_nodes`.
- `CHANGELOG.md` (this file).
- `README.md` legal disclaimer clarifying the script is an unofficial tool
  that is **not** sanctioned, endorsed, or provided by NetApp, Inc.
- `README.md` rewritten end-to-end from the project PDF (Overview,
  Prerequisites, Configuration File schema, Operation Modes, LOADER
  commands, CLI Reference, Step-by-Step, Session Logging, Debug/Screen/
  Background Mode, Known Issues, Troubleshooting, Changelog).

### Changed
- **Phase 2 optimizations (reliability + clarity):**
  - `_is_valid_ipv4()` now delegates the core check to
    `ipaddress.IPv4Address` while still rejecting leading-zero octets
    explicitly, so behavior is identical across Python versions
    (stdlib changed the leading-zero policy in 3.9.5).
  - 16 one-liner `try: X.close() / except Exception: pass` cleanup blocks
    between lines ~6848 and ~7485 converted to
    `with suppress(Exception): X.close()`. Intent is now explicit.
  - `from contextlib import suppress` added alongside the existing
    `contextmanager` import.
- **Phase 1 optimizations (quick wins, no behavior change):**
  - Hoisted `import ipaddress` to module top (was per-call inside
    `_parse_matching_gateway()`).
  - Hoisted shell-prompt regex to module level:
    `_SHELL_PROMPT_RE = re.compile(r"::\*?>")`. Used by `_shell_run_cmd()`
    instead of recompiling per call.
  - `_shell_run_cmd()` polling loop uses adaptive backoff: 10 ms while
    data is flowing, 100 ms when idle. Replaced the `while/else` exit
    construct with an explicit `got_prompt` flag (safer for future edits).
  - Dropped unused `from collections import OrderedDict`. `SessionLogger`
    uses plain `dict` for `_step_times` and `_step_counts` (Python ≥ 3.7
    dicts are insertion-ordered).
  - `_restore_terminal()` calls `stty sane` via
    `subprocess.run(["stty", "sane"], stderr=DEVNULL, stdout=DEVNULL,
    check=False)` instead of `os.system("stty sane 2>/dev/null")`.
    Catches `(FileNotFoundError, OSError)`. Removes the shell-injection
    surface and is portable to non-`/bin/sh` environments.

### Fixed
- **Mode 3 node-add status messages.** During parallel peer auto-add
  (option 3), the `Waiting for cluster creation` / `Still waiting for
  cluster creation...` messages incorrectly fired because
  `_auto_answer_disk_erase_prompts()` inferred node-add behavior solely
  from `_operation_mode == 2`. Added an explicit `is_node_add` parameter:
  - `True`  → "Waiting for node to boot and join the cluster." /
              "Still waiting for cluster join..."
  - `False` → "Waiting for node to boot and begin cluster creation." /
              "Still waiting for cluster creation..."
  Call sites updated:
  - 2b auto-join (`is_node_add=True`)
  - mode-3 peer worker (`is_node_add=True`)
  - mode 1b cluster init (`is_node_add=False`)
  Both branches now emit reporter messages and the log path (previously
  only the cluster-creation branch did).

## [v2] – 2026-05-15

### Added
- `--screen` flag: auto-launches the script inside a detached GNU `screen`
  session to protect against SSH disconnections and terminal timeouts.
  Implies `--bg`. Detects an existing screen session via the `STY` env
  var to prevent recursion.

## [v2b] – 2026-04-07

### Added
- Parallel peer node operations.
- End-to-end mode (3).
- ONTAP upgrade (4a).
- Netboot install (4b).
- License install (4c).
- SSH key setup (4d).
- Config backup (4e).
- BMC auth verify (4f).
- JSON config file support.
- Background mode (`--bg`).
- Session log with phase/step timing, warnings, and errors inventory.

## [v2a] – 2026-04-07

### Added
- Session logging with timing and summary.
- Warning/error collection in summary.

### Changed
- `_recv_loop` + thin wrapper architecture.
- Module-level `_peer_reinit_worker`.

## [v1] – 2026-04-07

- Initial release.
- Modes 1a and 2a.
