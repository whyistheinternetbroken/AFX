# Changelog

All notable changes to `AFX_reinit.py` are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project versioning follows the script's internal `v1`/`v2`/`v2a`/...
revision labels rather than strict [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **4a ONTAP upgrade — BMC picker from existing config.** When a reinit
  config (`primary_node.bmc` / `secondary_nodes[].bmc`) or `BMC_IP.json`
  is on disk, 4a presents a numbered picker (file → BMC) instead of
  asking the operator to type the BMC address. The selected file is
  promoted to the run-wide `_config_data` whenever it carries
  `primary_node` / `secondary_nodes` / `nodes` / `cluster`, so
  downstream steps can default from it without re-prompting.
- **4a cluster login reuses BMC credentials.** After the BMC SSH
  succeeds, the chosen `bmc_user` / `bmc_password` are published as
  `_primary_bmc_user` / `_primary_bmc_password` and tried automatically
  by `_attempt_console_cluster_login` when the `system console` reaches
  a `login:` prompt. If that fails, the script falls back to a one-shot
  `Cluster admin password for {bmc_user}:` prompt rather than asking for
  both fields.
- **4a parallel image install over per-node management IPs.** When the
  operator chooses parallel mode, the script harvests every
  `node_mgmt_ip` from the picked reinit config (and, when the picked
  file is BMC-only, scans every other reinit config on disk via
  `_find_config_files(deep_scan=True)`) and round-robin assigns the
  surviving IPs to the per-node update tasks. Each parallel worker
  SSHes to its assigned LIF instead of funneling every session through
  a single cluster-mgmt LIF.
- **4a parallel pre-flight validation.** Before launching the parallel
  storm, every candidate IP is probed with a 3 s TCP/22 connect plus a
  one-shot SSH authentication using the cluster credentials. A per-IP
  ✅ / ❌ table is printed. On any failure the operator is asked
  whether to proceed with only the healthy targets or fall back to the
  sequential console path.
- **4a failover wait — long-haul defaults and live ETA.**
  `_wait_for_failover_state` now polls every **3 minutes** for up to
  **30 minutes** (was 20 s × 10 min) and prints a single recurring
  `⏳ Waiting for {phase_label} on {node}  (elapsed Xs / remaining Ys)`
  status line. Phase labels (`takeover/giveback`, `node reconnect`) are
  passed from `_do_takeover_giveback`.
- **Interactive prompt-wait telemetry.** A `_tracked_input` wrapper on
  `builtins.input` records the cumulative wall-clock time the script
  spent blocked at interactive prompts, the count of prompts, the
  longest single wait, and any individual waits over 60 s. The session
  summary and standalone summary file emit:
  - `Time waiting for prompts (xN)  Ts (Mm)`
  - longest single wait,
  - one line per extended (≥ 60 s) wait, and
  - `Unaccounted time = max(0, total_elapsed − phase_sum − prompt_wait)`
  so multi-hour gaps where an admin walked away from a prompt no longer
  show up as unexplained drift in the per-phase report.

### Changed
- **4a console output suppression.** `_run_cluster_command` now respects
  a new module-level `_console_quiet` flag, and `_recv_loop` /
  `drain_channel` only write chunks to `sys.stdout` when not quiet. The
  full output still streams to the session log via
  `_session_log.log_console`. Every cluster-CLI invocation inside
  `_run_ontap_upgrade()` (image show, `promoted-dev-update`, validate /
  install, default-image verify, `storage failover show`, takeover,
  giveback, post-upgrade `version`) and `_wait_for_failover_state`'s
  polling loop is wrapped in `_suppress_console()`. The operator now
  sees decorative status lines only; the raw command echo, table rows,
  and `cluster::*>` prompts land in the log.
- **4a "Cluster management IP" prompt removed.** Replaced by the
  per-node mgmt-IP pool described above. A manual prompt only appears
  as a last resort when no `node_mgmt_ip` is found in any reinit
  config on disk.
- **4b reinit sub-mode parity.** Selecting reinit type 3 from 4b now
  prompts for physical-disk zeroing (matching the behaviour of direct
  1a / 1b / 3).

### Added (earlier in [Unreleased])
- **Mode 3 checkpoint coverage expanded.** Three additional phases are
  now recorded for the standalone end-to-end mode 3 (and reused by
  mode 4b+3 where they apply):
  - `primary_bootmenu_done` (global) — set when the primary node
    clears the ONTAP boot menu (option 9 for mode 1b/3, option 4 for
    mode 2) and the cluster setup wizard is about to begin. Written
    from both the direct `main()` boot-menu path and from
    `_run_4b_standalone`.
  - `cluster_formed` (global) — was already recorded by mode 4b at
    the end of `_run_4b_standalone`; now also recorded by the shared
    `_run_cluster_setup_wizard` post-create path so direct mode 1b /
    mode 3 runs from `main()` capture the same milestone.
  - `peer_option4_done` (per-peer, mode 3) — set in
    `_add_peer_node_thread` after the peer clears boot menu option 4,
    finishes format, and reaches the join barrier. The marker is the
    natural "destructive work is done" boundary for each peer and is
    intended to drive resume-skip wiring (deferred to a follow-up
    change so the marker timing can be observed on a real cluster
    first).
- Resume banners (mode 4b at startup and `_option3_init_checkpoint`
  for direct mode 3) display the new phases alongside the existing
  install_done / reinit_loader / peer_joined / cluster_formed lines.
- `CheckpointManager` class docstring updated to list every phase the
  script writes today.
- **Script renamed `AFX-reinit_v3.py` → `AFX_reinit.py`.** All documentation,
  embedded help (`--help` man page), and examples updated to the new name.
  Use `git log --follow AFX_reinit.py` to trace history across the rename.
- **Checkpoint & resume for mode 4b.** A new `CheckpointManager`
  persists run progress to `afx_checkpoint.json` (alongside the script,
  72 h TTL) so an interrupted 4b run — Ctrl+C, network blip, BMC banner
  stall — can be resumed without re-running destructive steps.
  Phases tracked: `install_done` (per node), `reinit_loader` (per node),
  `cluster_formed`, `primary_setup_done`, `peer_joined` (per peer, mode 3),
  `option3_complete`. The file is deleted automatically on successful
  completion.
- `--resume` CLI flag. Loads `afx_checkpoint.json`, displays the summary,
  and resumes mode 4b from the first unfinished phase. When every BMC IP
  is marked `install_done` the run skips Steps 2–6a (SSH / reset /
  netboot / install / option-6 boot menu) and jumps straight to Step 6b
  (reconnect to LOADER + `boot_ontap menu`). Peers already in
  `peer_joined` are skipped during the parallel mode-3 auto-add loop.
  When `primary_setup_done` / `option3_complete` is set from a prior
  run, the resume prompt warns that re-running will destroy the existing
  cluster and asks for explicit confirmation.
- `--checkpoint-status` CLI flag. Prints a human-readable summary of the
  saved checkpoint — absolute file path, run mode, created/updated
  timestamps, age, log directory, config path, BMC IPs, completed
  global phases, and completed per-node phases keyed by BMC IP — then
  exits. Does not modify the checkpoint file.
- `_collect_license_config(ctx)` now runs from mode 4b for auto-setup
  flows (mode 1b / 3), not just from direct mode 1/3 entry points.
- `_prompt_bmc_host(prompt_text, allow_blank=False)` helper — single
  source of truth for BMC hostname/IP prompts. Retries on
  `_check_bmc_reachable` failure instead of immediately aborting.
  Reused by all six BMC-prompt entry points (4b, 4c, 4d, 4e, 4f, and
  the standalone reinit path).
- **Per-node join timing breakdown (mode 3).** The session summary now
  splits the old `Parallel Peer Auto-Add (mode 3)` phase into two phases —
  `Parallel Peer Option 4 (mode 3)` (parallel LOADER/format/option-4 work
  up to the join barrier) and `Node join total` (the sequential join phase) —
  and emits one indented sub-row per peer:
  `- Node [<name> - <BMC IP>]   <secs>s (<mins>m)`. Node name is taken from
  the matching `secondary_nodes[].name` entry in the config (falls back to
  just the BMC IP when not present).
- **Join → all-nodes-healthy wall time (mode 3).** A new indented sub-row
  is appended under `Node join total` reporting the wall-clock elapsed
  from the moment the first peer sends `join` to the moment the last
  peer's `cluster show` health check confirms the final cluster size with
  all nodes healthy. Rendered as
  `- Join → all nodes healthy   <secs>s (<mins>m)`.
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
- **Mode 4b `install_done` checkpoint missed on real completion paths.**
  `_opt6_login_nodes.add(ip)` (which drives the `install_done` checkpoint
  write) only ran on the "already at login" skip branch. The two real
  option-6 completion branches (reboot-then-login and direct-to-login)
  did not record progress, so `--resume` always re-ran Steps 2–6a.
  All three completion branches now record the marker under
  `connect_lock`.
- **Mode 4b Step 6b reconnect-to-LOADER fragile.** `_reconnect_worker`
  now wraps `_bmc_reach_loader` in a 3-attempt × 60 s backoff retry
  loop honoring `_shutdown_event`. Failure now reports
  "Reconnect to LOADER failed after 3 attempts." instead of bailing on
  the first banner timeout.
- **BMC banner retry too aggressive.** `_bmc_reach_loader` banner retry
  extended from 2 × 60 s to 5 × 60 s (matches real-cluster recovery
  windows) and stops re-prompting for credentials mid-reconnect by
  calling `_ssh_connect_with_retry(..., interactive=False)`.
- **Mode 4b dropped into `InteractiveSession` instead of running auto
  setup.** Root cause: `apply_to_globals()` was clobbering recent
  global writes made by legacy in-flow code. Helpers
  `_discover_and_prompt_config`, `_collect_license_config`, and
  `_option3_init_checkpoint` now call `ctx.refresh_from_globals()` on
  entry so the ctx mirror matches the current globals before any
  writes happen.
- **BMC reachability prompts aborted on a single failure.** All six
  BMC-prompt entry points now route through `_prompt_bmc_host`, which
  re-prompts until the host responds or the user cancels.
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
