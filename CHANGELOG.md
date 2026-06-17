# Changelog

All notable changes to `AFX_reinit.py` are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project versioning follows the script's internal `v1`/`v2`/`v2a`/...
revision labels rather than strict [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **Node-add manifest files now archived to run log directory on success.**
  After successful completion of mode 2b (parallel add), mode 3 (end-to-end),
  or mode 4b with reinit enabled, node-add manifest files from `configs/`
  (`node_add_manifest_<ts>.json` and `last_node_add_manifest.json`) are moved
  into the run's timestamped log directory (`logs/<timestamp>/`). This ensures
  manifests used for resume are preserved alongside that run's logs.
  The `last_node_add_manifest.json` pointer is updated to reference the new
  archived location so resume workflows remain functional.
- **Run summary now includes ONTAP version before/after snapshots.**
  Summary output now records ONTAP version state when available and prints
  it in the Result section as "ONTAP before run" and "ONTAP after run",
  including per-node version lines for each snapshot. Upgrade flow (4a)
  records pre-upgrade and post-upgrade node versions; 5g health checks
  record current per-node versions for both fields.
- **Checkpoint status now reports current phase.**
  The checkpoint JSON now stores a live `current_phase` field (phase name,
  state, timestamp, optional node), updated during phase transitions.
  `--checkpoint-status` now prints this so operators can see where a job is
  currently running.
- **Live run summary now exists from startup and updates per phase.**
  The session summary file is now created when a run starts (not only at
  shutdown) and refreshed as phases start/end/record outcomes. In-progress
  or incomplete phases are explicitly labeled as not yet completed, so
  `--last-status` can show meaningful status while a job is still running.
- **Option 3 is now reinit-only (no install-first path).**
  Choosing ONTAP image install from option 3 now prints guidance to use the
  install menu options and returns to the main menu. ONTAP install workflows
  remain in 4b/4c, while option 3 assumes the desired ONTAP version is already
  installed.
- **SSH remediation now includes known_hosts reset (`ssh-keygen -R`).**
  Added a dedicated "Remove BMC from known hosts" action in mode 5h and
  integrated the same step into post-failure SSH remediation used by modes
  1–4 (via the shared diagnostics helper).
- **5c gather/build now writes `configs/cluster_IP.json` automatically.**
  When mode 5c connects to an existing cluster (gather path or build/add path),
  it now captures cluster-role interface IPs and writes `cluster_IP.json` in
  addition to the config snapshot output.
- **Added mode 4c for install-only netboot image deployment.**
  New mode `4c` reuses the 4b netboot/install pipeline but always stops after
  ONTAP image install, skipping reinit and any cluster create/node-add flows.
- **Cluster health gates now validate cluster-port link/health state.**
  End-of-run health checks (modes 1–4) and utility mode 5g now run
  `network port show -ipspace Cluster` and fail when any cluster port is not
  `Link=up` or not `Health=healthy`, with detailed per-node/per-port warnings.
- **Added mode 5l to build `configs/cluster_IP.json` for node-add reuse.**
  New utility option `5l` connects to cluster management, queries
  cluster-role interfaces (`-role cluster`), and writes an ordered cluster-IP
  manifest used by node-add flows.
- **Node-add IP ordering now prefers `configs/cluster_IP.json`.**
  Options 2a/2b/3/4b now use manifest order for `cluster add-node -cluster-ips`
  when available, with runtime-collected IPs appended if missing from file.
- **`cluster_IP.json` now records one cluster IP per node.**
  The 5l writer keeps the first cluster-role IP returned per node (in command
  output order) to avoid dual-cluster-LIF duplication in add-node arguments.
- **Modes 1–4 now evaluate AUTOBOOT during boot-DNA review.**
  During LOADER boot-DNA verification, the script now checks `AUTOBOOT` and,
  when it is `false`, asks once whether to force `AUTOBOOT=true` after
  `set-defaults`. If approved, `setenv AUTOBOOT true` is injected with the
  post-default LOADER bootarg commands.
- **Password groups added for per-node credential collection.**
  In same-password prompts across node/BMC workflows, choosing per-node
  passwords now offers a password-group mode that lets operators
  assign one password to numbered subsets of nodes, review a manifest, and
  restart grouping before continuing.
- **2a/2b/3 now include the same blank-password fallback used by 4b.**
  BMC SSH connect/reconnect paths in options 2a, 2b, and 3 now silently try
  fallback credentials (including blank password) before prompting again.
- **4b now probes longer for LOADER before issuing resets.**
  In option 4b LOADER transitions (initial reset and reconnect), the pre-reset
  LOADER probe timeout was increased and now explicitly skips `system reset`
  when the node is already at LOADER.
- **Help and in-app mode labels now match current option numbering.**
  Updated CLI/help references from legacy `4f/4g/5e` wording to current
  `5d/5z` naming, and refreshed the 5k man-page label to describe its
  config-driven, state-aware boot-DNA behavior.
- **Modes 1–4 now fail fast globally on unsupported boot DNA.**
  If any node reports an unsupported `bootarg.init.dna` value during reinit
  workflows, the run now stops all node work, aborts the script, and tells the
  operator to contact NetApp Support before retrying.
- **Added 5k boot-DNA check utility for live clusters and LOADER.**
  Option 5 now includes a `5k` utility that accepts either a cluster
  management IP or a BMC IP, detects whether the target lands at a cluster
  shell or LOADER, and reports `bootarg.init.dna`. The LOADER path uses
  `printenv bootarg.init.dna`, while the live-cluster path runs
  `node run * -c "priv set diag; bootargs get bootarg.init.dna"`.
- **5k target selection now uses config-driven numbered options.**
  The boot-DNA utility now loads BMC and cluster-management targets from JSON
  config and offers a numbered picker: **1)** all BMC IPs, **2)** cluster
  management IP, **3)** custom IP/hostname.
- **5k now reports per-target node state in multi-node results.**
  When checking all BMC targets, 5k now evaluates each node's runtime state
  (At LOADER vs At cluster shell), uses the matching bootarg query path, and
  prints a summary list that includes both node state and DNA value(s).
- **5d rerun can now reselect targets from the numbered BMC list.**
  When an operator chooses to rerun BMC auth verification, the script now
  reopens the numbered target picker so a different all/subset selection can be
  tested without returning to the main menu.
- **5z reset-to-LOADER now supports numbered subset selection.**
  The LOADER target list is now shown with numeric indices, and operators can
  run the reset against all targets or a comma-separated subset (for example,
  `1,3,4`) before credentials are collected.
- **Unhandled crashes now write a dedicated traceback log file.**
  On unhandled exceptions, the script now writes a full stack trace to
  `crash_trace_<timestamp>.log` under the run's `logs/<timestamp>/` directory
  (or a new logs timestamp directory if no session log was active yet).
- **Peer-node password prompts now reserve blank for blank passwords.**
  In per-node credential prompts (modes 2a/2b and shared peer-credential
  flows), blank input is now treated as an intentional blank password. To reuse
  the primary password, operators now enter `PRIMARY` explicitly.
- **2b primary-BMC prompt now clarifies credential-context behavior.**
  The interactive 2b primary-BMC prompt now states that this node is used as
  the primary-password context for peers and is not the cluster primary node.
- **5h SSH diagnostics single-target selection now uses a labeled config-IP picker.**
  When choosing "one" target for SSH diagnostics, the script now shows a
  numbered list of IPs discovered from config (BMC, cluster management, and
  node management) with labels, and also allows entering a custom IP/hostname.
- **`--help` man page updated to match current CLI options.**
  The built-in help output now includes the full current flag set
  (`--auto-clear-stale-bmc`, `--diag`, and all mode shortcut flags) and uses
  current menu numbering for utility modes (`5a`–`5e`).
- **5d BMC auth verify now supports numbered subset selection.**
  The BMC target list is now shown with numeric indices, and operators can run
  verification against all targets or a comma-separated subset (for example,
  `1,3,4`) before credential prompts and test execution.

### Fixed
- **2b reconnects no longer send `system console` at LOADER.**
  In option 2b peer-node reconnect loops, reconnect state is now probed first
  and `system console` is only sent from a BMC prompt. LOADER/boot-menu states
  now resume directly without issuing invalid LOADER commands.
- **System-console commands are now gated to BMC prompts.**
  LOADER probe paths no longer send `system console` when already at LOADER or
  other non-BMC prompts, preventing repeated `system console` spam on LOADER.
- **4b no longer stalls after detecting an existing LOADER prompt.**
  In the parallel reset-to-LOADER step, nodes already at LOADER now proceed
  immediately instead of waiting for reboot/AUTOBOOT output that never arrives.
- **2b/2a parallel worker failures no longer cascade when one node aborts.**
  A non-interactive per-node LOADER/DNA failure now aborts only that worker
  instead of closing the global session log. SessionLogger write methods now
  safely no-op after close, preventing `ValueError: I/O operation on closed file`
  from concurrent worker threads.
- **`--help` output rendering on non-UTF consoles.**
  The help-page horizontal rule now uses ASCII characters so `--help` does not
  fail with Unicode encoding errors on Windows/code page terminals.
- **5h stale-SSH diagnostics now support per-IP targeting and ipmitool-only runs.**
  Operators can now choose to run diagnostics/cleanup against all loaded BMCs
  or a single selected IP. Added an explicit `ipmitool sol deactivate` action
  in the 5h menu, in addition to full stale-session cleanup.
- **5d failure diagnostics now support per-IP targeting and ipmitool-only runs.**
  The post-failure diagnostics helper now allows selecting all failing BMCs or
  one IP, and includes an `ipmitool sol deactivate`-only pass before optional
  full cleanup.
- **Blank-password credential retries now require explicit skip.**
  SSH credential re-prompts now treat blank passwords as intentional retry
  values across the shared retry helper and mode 2a/2b pre-auth flows. To stop
  retrying, operators must enter `SKIP` explicitly.
- **5d/5g now pause before returning to menu.**
  After BMC auth verify (5d) and cluster health/version check (5g), the script
  now prompts `Press Enter to return to the main menu...` so result output
  remains visible until the operator acknowledges it.
- **4b skip prompts and continue prompt wording simplified.**
  The LOADER backup/printenv skip prompts no longer include the parenthetical
  "DNA check still runs". 4b continue prompts now read
  `Continue with reinit of entire cluster? [y/n]:` and are asked once at
  cluster scope.
- **VLDB-timeout path is now cluster all-or-nothing.**
  Per-node `Continue with reinit?` prompts were removed from the option-6
  worker flow. On VLDB timeout, workers proceed directly and the single
  cluster-level continue gate determines whether reinit continues.
- **Default BMC username prompt shown when loading config BMC list.**
  The fallback prompt after loading a BMC config file now reads
  `BMC username [admin]:` so pressing Enter clearly keeps `admin`.
- **4b pre-collected cluster admin password now requires confirmation.**
  When pre-collecting cluster admin password because one or more BMC passwords
  are blank, the script now asks `Confirm cluster admin password` and retries
  on mismatch before storing the value.
- **LOADER prompt detection now keys on `LOADER-` during automation/polling.**
  Prompt waits used by LOADER command execution, netboot, boot-menu staging, and
  status checks were aligned to look for `LOADER-`/`loader-` instead of generic
  `LOADER`/`loader` text, reducing false-positive matches in noisy console output.
- **4a upgrade uses cluster-mgmt LIF SSH as primary channel; BMC is fallback only.**
  The 4a ONTAP upgrade workflow now attempts a direct SSH connection to the
  cluster management LIF (sourced from `reinit-config.json` or prompted at
  startup) before falling back to the BMC console. All ONTAP CLI commands
  (image show, promoted-dev-update, image update, failover show, version
  verify, health checks) run over the clean SSH channel, eliminating the ANSI
  / VT100 noise and PTY echoing from the BMC PTY that caused output-parsing
  failures. BMC is opened only if the cluster-mgmt LIF is unreachable. The
  active channel is exposed as `_cl_ch`; `channel_41` / `client_41` are only
  set when the BMC path is taken.
- **Final cluster health check (Step 11c) uses cluster-mgmt LIF SSH.**
  `_wait_for_cluster_healthy` now receives a fresh SSH session to the
  cluster-mgmt LIF (`_sfo_poll_ip`) instead of `channel_41`. This ensures
  `_parse_failover_show` sees clean ONTAP output and can correctly determine
  whether all nodes have returned to `Connected to <partner>` state after the
  rolling upgrade. Falls back to `channel_41` only if no cluster-mgmt IP is
  available.
- **Post-upgrade version verify (Step 12) uses cluster-mgmt LIF SSH.**
  Step 12 now opens a dedicated SSH session to `_sfo_poll_ip` for the
  `system image show -fields version,is-current` command. Running version is
  extracted from the `is-current=true` row rather than parsing the `version`
  command (more reliable for RC and special builds). Falls back to `channel_41`
  if no cluster-mgmt IP is available.
- **Menu labels for env tools marked experimental.**
  Options `5i` and `5j` now display `(experimental)` in both the main menu and
  the option-5 sub-menu.

### Fixed
- **Mode 4b reconnect worker crash on tuple unpack.**
  `_bmc_reach_loader` now consistently returns a 3-tuple
  `(client, channel, failure_reason)` in all paths, fixing
  `ValueError: not enough values to unpack (expected 3, got 2)` during
  reconnect-to-LOADER handling.
- **Option 2b/3 boot-menu stalls on `Waiting for BMC`.**
  During boot-menu wait, when `Waiting for BMC` appears and console output then
  goes silent for 60s, the script now performs a visible BMC SSH + system-console
  reconnect and resumes waiting on the refreshed channel instead of silently
  hanging.
- **Version parse failure after upgrade.** Step 12 was using `channel_41`
  (BMC PTY), which injected spurious `Password:` prompts and ANSI escape codes
  into the command output, causing the version regex to fail and print
  `(parse failed)`.
- **Final health check always reporting "not found in failover show output".**
  `_wait_for_cluster_healthy` was running over the BMC PTY (`channel_41`),
  which produced ANSI/VT100-corrupted output that `_parse_failover_show` could
  not parse, returning zero rows. Switching to a direct SSH session resolves
  the corruption.

### Added
- **Startup CLI flag completion support.**
  The parser now integrates with `argcomplete`, so startup flags like
  `--reinit`, `--config`, and `--screen` can be Tab-completed when the shell
  completion hook is enabled.
- **Runtime pause/resume control built into live runs.**
  Added operator-controlled runtime pause that can be triggered by sentinel
  file (`.afx_pause`) or Unix signals (`SIGUSR1` toggle, `SIGUSR2` resume).
  While paused, console automation loops hold position and auto-reconnect /
  reclaim behavior is suppressed so manual BMC console work can proceed
  without the script taking over.
- **Runtime manual checkpoint trigger for active runs.**
  Added manual checkpoint snapshots during live execution via sentinel file
  (`.afx_checkpoint_now`) or Unix signal (`SIGURG`). Each request writes a
  timestamped snapshot under `checkpoints/` as
  `afx_checkpoint_manual_YYYYMMDD_HHMMSS.json`.
- **Option-4 zero-disks wait now retries option 4 if boot menu is still visible.**
  In disk-erase auto-answer flow, the script now detects an unchanged
  `Selection (1-N)?` boot menu during "zero disks confirmation" wait and
  automatically re-sends option `4` (up to three attempts) to avoid a
  long stall waiting for prompts that cannot appear.
- **Boot-menu CR keepalive every 5 minutes.**
  While waiting for the ONTAP boot menu, the script now sends a periodic carriage
  return every 300 seconds to help keep BMC console sessions active during long
  waits.
- **Boot DNA check captures full LOADER `printenv` to `configs/`.**
  DNA verification now runs `printenv` (not `printenv bootarg.init.dna`) and
  writes raw output to `configs/loader_printenv_<timestamp>.txt` for review,
  then parses `bootarg.init.dna` from that capture.
- **Pre-reinit abort prompt after LOADER env diff.**
  After showing the pre/post `set-defaults` env diff during reinit flows, the
  script now asks whether to exit before proceeding with further boot changes.
- **Option 5g: list and clean up stale BMC SSH sessions.** A new menu option
  under category 5 that diagnoses stale SSH socket connections to BMC/SP
  addresses, deactivates stuck SOL sessions via `ipmitool sol deactivate`,
  and optionally SIGTERMs stale prior-run Python processes holding open TCP
  connections to the BMC. Presents an interactive loop with options to list,
  clean up, or exit back to the main menu.
- **All option 5 jobs return to the main menu after completion.** Options 5a–5g
  now raise `_ReturnToMenu` instead of calling `sys.exit()`, so the operator
  is returned to the main menu after each standalone utility finishes (success
  or failure) rather than the script exiting.
- **Option 5f: standalone cluster health and version check.**A new menu option
  under category 5 that connects to the cluster management LIF via SSH, runs
  `cluster show`, `storage failover show`, and `system image show`, and reports
  whether the cluster is healthy and which ONTAP version all nodes are running.
  Suitable for a quick post-upgrade or ad-hoc health verification without
  running a full upgrade workflow.
- **5f auto-loads connection details from `reinit-config.json`.** On startup,
  5f searches for a `reinit-config.json` in the standard config directories and
  pre-populates the cluster management LIF IP and username. If no config is
  found it offers to launch the 5c config-gather workflow first, then returns
  to the health check automatically after the config is written.
- **Numbered upgrade mode prompt.** The `4a` upgrade workflow now presents
  `validate`, `install`, and `prestage` as a numbered list (1/2/3). Both the
  number and the keyword are accepted as valid input.
- **`update-docs` skill (`.github/skills/update-docs/`).** A repo-level agent
  skill that guides updating `README.md` and `CHANGELOG.md` after changes to
  `AFX_reinit.py`. Automatically triggered when the user asks to update docs,
  readme, or changelog.

### Fixed
- **BMC SSH "Not allowed at this time" now retried instead of failing.** The SP/BMC
  firmware rejects SSH connections with this message when it is busy serving another
  session. `_ssh_connect_with_retry()` now treats this as a transient banner-class
  error, applies the same 60 s × 5 retry logic already used for banner timeouts, and
  displays a clearer "BMC SSH not ready (banner timeout or SP busy)" message. No
  manual retries required.
- **5f `_json` NameError on config read.**The config file loader in mode 49
  was calling `_json.load()` instead of `json.load()`, raising a `NameError`
  on any run where a `reinit-config.json` was found.
- **5f → 5c redirect returned to summary log instead of resuming 5f.** Setting
  `_operation_mode = 46` and returning was a no-op because the dispatch block
  had already passed. Fixed by adding a `_5f_pending_after_4e` flag (mirroring
  the existing `_4a_pending_after_4e` pattern); when 5c completes it checks the
  flag and falls through to the mode 49 block instead of calling `sys.exit(0)`.
- **"Option 4e" label in no-config prompts.** Two prompts that referred to the
  config-gather workflow as "Option 4e" were updated to "Option 5c" to match
  the current menu numbering.

### Changed (prior)
- **Menu reorganized into two install/admin categories.**
  - Category **4 "Install ONTAP"** now contains only `4a` (ONTAP upgrade) and
    `4b` (netboot install).
  - New category **5 "Administration and maintenance"** contains `5a` (install
    license), `5b` (SSH key setup), `5c` (config backup, formerly `4e`), `5d`
    (BMC auth verify, formerly `4f`), and `5e` (reset to LOADER, formerly `4g`).
  - Exit moved from option `5` to option `6`.
  - Typing `4` or `5` at the main prompt now shows the respective sub-menu;
    blank Enter returns to the main menu.
- **`BMC_IP.json` listed first in config file picker with blank-enter default.**
  When multiple config files containing BMC entries are found, `BMC_IP.json`
  is always sorted to the top and marked `(default)`. Pressing Enter without
  typing a number selects it automatically.
- **Cluster-mgmt LIF prompt when multiple LIFs found.** During option `5c`
  config gather, if more than one LIF with `cluster-mgmt` role is detected the
  operator is now prompted to choose which one to use.

### Fixed
- **Takeover/giveback monitoring rewritten with `-fields` poll.**
  `storage failover show` is now called with
  `-fields state-description,takeover-of-possible,takeover-by-possible`
  for structured, unambiguous parsing. Two-phase logic: Phase 1 waits for
  "Waiting for giveback" then issues giveback; Phase 2 waits for
  `takeover-of-possible=true`, `takeover-by-possible=true`, and state not
  containing "Waiting for cluster applications to come online". SSH reconnect
  is attempted automatically if the channel drops during polling.
- **PTY column width set to 256 to prevent ONTAP table wrapping.** The SSH
  channel PTY is resized to 256 columns immediately after opening, preventing
  ONTAP's `-fields` output from wrapping at 80 characters and causing field
  values (`takeover-by-possible`, `state-description`) to parse as `None`.
- **Current state included in takeover/giveback elapsed/remaining output.**
  Poll lines now show `; Current state: <state>` so the operator can see
  the node's state while waiting.
- **`LOADER will appear` message suppressed during `4a` upgrades.**
  `enter_system_console()` now accepts a `loader_message` parameter (default
  `True`). The upgrade caller passes `loader_message=False` since nodes reboot
  directly back into ONTAP without stopping at LOADER.
- **Node-row matching in `_wait_for_failover_state` fixed.** The previous
  `node in line` substring check matched the node name appearing in partner
  nodes' indented continuation lines. Now only non-indented lines that start
  with the node name are considered.

### Previously Added
- **Screen output log (`screen_output_*.log`).** Every line printed to the
  operator's terminal during a run is now mirrored to a
  `screen_output_<timestamp>.log` file inside the session log directory.
  ANSI escape codes are stripped so the file is clean plain text. The new
  `_TeeStdout` class wraps `sys.stdout` transparently; the original stdout
  is restored when `SessionLogger.close()` is called.
- **Auto-offer option 4e when no config files are found (modes 1/3).** When
  mode 1 (initialize first node) or mode 3 (end-to-end reinit) is selected
  without a `--config` flag, and no `reinit-config.json` or `BMC_IP.json`
  is detected in the standard search paths, the script now asks:
  _"Would you like to generate them from an existing cluster (option 4e)?"_
  Answering Y (the default) redirects into the option 4e gather-config flow
  so the user can pull the cluster configuration before starting the reinit,
  without having to restart the script and manually pick 4e.
- **`cluster add-node` bulk join flow (modes 2a, 2b, 2c, 3, 4b).** Peer node
  addition has been re-architected around ONTAP's native bulk join command.
  The previous per-node interactive cluster-join wizard flow (create/join
  prompts, `join_barrier` synchronization, serialized wizard answers) has been
  **removed**. Now all peer nodes run Option 4 / disk erase / node-mgmt config
  in parallel, then Ctrl+C is sent to abort the wizard, the node logs in as
  `admin`, and `net int show -role cluster -fields address` captures one
  cluster-interface IP per node. Once all parallel threads complete, the primary
  node issues a single `cluster add-node -cluster-ips IP1,IP2,...` command that
  adds all peers simultaneously. Progress is monitored with
  `cluster add-node-status` polled every 120 seconds (up to 15 minutes).
- **Per-node milestone timing in session summary (modes 2a, 2b, 3, 4b).** The
  parallel peer phase now emits five timestamped sub-rows per node: LOADER
  reached, Option 4 sent, disk erase done, node-mgmt applied, and cluster IP
  captured. Each time is seconds elapsed from thread start. The `cluster add-node`
  success time per node is also tracked and reported.
- **`--skip-broken` fallback for system package installs.** When a `dnf`/`apt`
  system package install fails, the script now falls through to `pip install`
  instead of calling `sys.exit(1)`. The `pip` fallback was already present;
  this change ensures it is always reached on system-install failure.
- **NTP server picker always shown during cluster setup collection.** When
  `collect_cluster_config` is called (modes 1b, 3, 4b), the NTP picker is now
  always displayed even if `ntp_servers` is already set in the config file.
  The current config value is shown above the picker; pressing Enter (blank)
  keeps the existing value while selecting new servers replaces it.

### Fixed
- **Primary BMC leaked into peer list when hostname and IP differ.** `sp_host`
  is stored as the IP address entered at startup, but a config file may list the
  same node's BMC by hostname (or vice versa). The exact-string `bmc == sp_host`
  check silently failed, causing the primary node to be included in the
  `cluster add-node -cluster-ips` command and appear as a node being "added" to
  its own cluster. Fixed by resolving both `sp_host` and each candidate BMC to
  IP via `socket.gethostbyname()` before comparing. A second safety filter was
  also added inside `add_peer_nodes_parallel` (accepts `primary_bmc=` parameter)
  to catch any entry that resolves to the primary IP before threads are spawned.

### Added
- **Pause wait row in run summary.** When a run is paused at least once (via
  `.afx_pause` file or `SIGUSR1`/`SIGUSR2` signals), the Phase Timing section
  of the session summary now includes a dedicated `Pause wait (xN)` row showing
  the total time held in pause and the number of pause events. A `longest single
  pause` sub-line records the duration and the context label (e.g., "boot menu
  wait") of the longest individual pause.
- **Per-node netboot download/install subtimings.** Under the netboot install
  phase (`4b – Netboot Install`, `Peer Netboot Install`, `Netboot ONTAP
  Install`), each node now contributes indented `[<node>] image download` and
  `[<node>] image install` sub-rows in the Phase Timing report. This makes it
  easy to identify slow nodes or transfer bottlenecks across multi-node installs.
- **`Auto Join` phase for cluster-join attribution.** The peer cluster-join
  wizard (mode 2b / mode 3) is now recorded as a named `Auto Join` phase. Long
  cluster-join waits that previously appeared as unaccounted time in the phase
  summary are now attributed to this phase.
- **Menu option 4g: Reset all nodes to LOADER prompt.**Connects to all BMC
  addresses (from config file or manual entry) in parallel, issues a system
  reset on each node, enters the system console, and sends Ctrl+C to interrupt
  AUTOBOOT. The script exits with a pass/fail results table once every node has
  reached the LOADER> prompt. Node-level logs are written to the session log
  directory. Available as `--loader` CLI flag.
- **CLI mode shortcut flags.** Nine flags bypass the interactive menu:
  launch directly into the requested mode: `--first-node` (1b), `--add-nodes`
  (2b), `--reinit` (3), `--netboot-install` (4b), `--add-lic` (4c),
  `--passwordless` (4d), `--backup` (4e), `--verify` (4f), `--loader` (4g).
  All flags can be
  combined with `--config`, `--debug`, `--screen`, and other existing options.
  See *Mode Shortcut Flags* in the README for examples.
- **Incremental node join timing.** The per-node sub-rows under `Node join
  total` in both the session log and the standalone summary file now show
  **incremental** elapsed time for the 2nd and later nodes (e.g. `+10.2m`)
  rather than cumulative time, making it easy to see how long each individual
  node join took. The first node and the `Join → all nodes healthy` line
  retain their cumulative totals.
- **Periodic elapsed-time heartbeat during cluster health wait.** While
  `_wait_for_cluster_nodes_healthy` polls every 5 minutes, the terminal now
  prints a `⏳ Still waiting for N healthy node(s) — elapsed Xm Ys; next
  check in ~5 min...` line before each sleep so operators can confirm the
  script is alive during long waits.

### Changed
- **`--diag` bootarg validation broadened.** Bootarg entries no longer need
  to start with `bootarg.` — any `option_name value` two-token pair is
  accepted. Invalid entries (missing value, `setenv` prefix) are now a hard
  exit rather than a warn-and-skip prompt.
- **`--diag` bootarg confirmation prompt.** After loading bootargs (from file
  or interactive input), all entries are printed as `setenv option value` and
  the operator must confirm before the script proceeds.
- **`--diag` and physical-zeroing prompts moved.** Both questions (physical
  disk zeroing and diagnostic bootargs) now appear right after the config file
  / retain prompts and before any BMC connection, grouping all up-front
  questions together.
- **`bootargs.txt` / `bootargs` search path extended.** The script now checks
  `configs/bootargs.txt`, `configs/bootargs`, `./bootargs.txt`, and
  `./bootargs` in that order, so the file can live alongside other config
  files in the `configs/` subdirectory.


- **DSA host key rejection (`q must be exactly 160, 224, or 256 bits long`).**
  `cryptography` ≥ 2.6 strictly validates DSA `q` parameter lengths. Some
  ONTAP BMC and cluster management interfaces present non-standard DSA host
  keys that fail this check. Fixed by adding
  `disabled_algorithms={"pubkeys": ["ssh-dss"]}` to every `SSHClient.connect()`
  call site in the script so DSA host key negotiation is skipped entirely and
  paramiko falls back to RSA/ECDSA.
- **Raw BMC console output leaked to terminal during `system console` entry.**
  BIOS version banners, copyright text, and memory-init lines were printed to
  the operator's terminal between "System console connected" and "Now
  monitoring boot output" because `_recv_loop` and `drain_channel` wrote raw
  channel data directly to `sys.stdout` before `_NodeLogWriter` was installed.
  Added `quiet=True` to all `direct_read_until_any` and `drain_channel` calls
  inside `enter_system_console()`. Console data continues to flow to the
  session log; only the terminal display is suppressed.

### Added
- **4e config gather — cluster management SSH support.**The 4e "backup
  configuration" entry point now accepts a BMC address, cluster management
  IP, or cluster hostname as the connection target. A ping check validates
  the entered address before proceeding; re-entry is requested on failure.
  When a reinit config file is loaded, `primary_node.bmc` is pre-filled
  automatically so no prompt is shown.
- **4e config gather — NTP server capture.** `collect_retain_data` now runs
  `ntp server show` and stores the result in `_retained_ntp_servers`.
  `apply_retained_to_cluster_config` writes a `ntp_servers` field (comma-
  separated) to the config. If no NTP servers are found and the config has
  no existing `ntp_servers` entry, the operator is offered `pool.ntp.org`
  as a one-prompt default.
- **4e config gather — LIF summary split by type.** The retained
  configuration summary now displays two separate tables instead of one
  combined table:
  - **Cluster LIFs** (IPspace = Cluster): `lif`, `home-node`, `port`,
    `address`, `netmask`
  - **Management LIFs** (roles `node-mgmt` / `cluster-mgmt`): same
    columns plus a `role` column
  Both tables use fixed-width columns with dash separators sized to the
  actual content width.
- **"Same credentials for all peers" prompt (modes 1/3).** Before
  prompting for individual peer BMC credentials, the script now asks:
  _"Use the same BMC username 'admin' and password for all peer nodes?
  [Y/n]"_. Answering Y (the default) silently assigns the primary node's
  credentials to every peer, avoiding repeated Enter presses for clusters
  where all BMCs share the same login. Answering N falls through to the
  existing per-node prompt loop (Enter still reuses primary credentials).

### Changed
- **Option 3 / 4e BMC prompt text.** The "Enter SP hostname/IP" prompt now
  reads "Enter BMC hostname/IP or primary node (this will be the first node
  in the cluster)" to clarify that a cluster management address is also
  accepted.
- **Default BMC username is `admin` in options 3 and 4d.** Both prompts
  now read `BMC username [admin]:` and fall back to `admin` when the
  operator presses Enter without typing a name.
- **4e summary table dash separators.** Separator lines in the Cluster LIF
  and Management LIF tables have been extended to match the actual column
  widths (89 characters for Cluster LIFs, 105 for Management LIFs).

### Fixed
- **4e config gather — `_parse_network_interfaces` silently undefined.**
  An edit that inserted `_parse_ntp_servers` accidentally removed the `def`
  line for `_parse_network_interfaces`, leaving its body as unreachable dead
  code inside the NTP parser. All calls to `_parse_network_interfaces` would
  raise `NameError` at runtime, so the captured network interface rows were
  always empty — causing the saved config to omit `clus_mgmt_address`,
  `clus_mgmt_mask`, `mgmt_port`, and all per-node management data. The `def`
  line has been restored.
- **4e config gather — spurious cluster login and wrong exit on direct
  cluster SSH.** When connecting directly to cluster management (rather than
  via BMC console), `collect_retain_data` was calling `_wait_for_cluster_prompt`
  which attempted a cluster login (sending `admin\r` as a cluster command)
  and then sent Ctrl+D followed by a BMC prompt wait that never arrived.
  Fixed by adding a `direct_cluster_ssh=True` code path that skips console
  entry/exit entirely and closes the session with `exit`.
- **4e config gather — wrong BMC address written to `primary_node.bmc` when
  connecting via cluster management IP.** The cluster management hostname/IP
  was being written as the primary node's BMC address. The script now
  matches each SP IP from `service-processor show` against the configured
  `node_mgmt_ip`; the matching entry becomes the real primary BMC, with a
  fallback to the first SP IP when no match is found.
- **`RunContext` `TypeError` for `retained_ntp_servers`.** The new
  `retained_ntp_servers` checkpoint key was added to `_SYNC_KEYS` without a
  corresponding field in the `RunContext` dataclass, causing `TypeError:
  __init__() got an unexpected keyword argument 'retained_ntp_servers'` on
  startup. The field has been added.
- **4e config gather — `net int show -instance` parser failures.**
  Several cascading issues prevented `_parse_network_interfaces` from
  returning any rows, causing `reinit-config.json` to be written without
  `primary_node` or `secondary_nodes` blocks:
  - **ANSI escape codes in PTY output.** `invoke_shell()` injects VT100
    sequences (e.g. `\x1b[m`) into "blank" separator lines between
    `-instance` records. `str.strip()` does not remove them, so the blank-
    line detector never fired and blocks were never committed. Fixed by
    stripping ANSI codes with `_ANSI_RE` before parsing, and by checking
    `ch.isprintable()` per-character when detecting blank separators.
  - **`(DEPRECATED)-Role` label filtered.** The parser skipped every line
    starting with `(` to ignore the `(network interface show)` command-echo
    header. This also silently dropped the `(DEPRECATED)-Role: cluster`
    field, so no LIF was ever classified as a Cluster or node-mgmt
    interface. Fixed by requiring BOTH `startswith("(")` AND `endswith(")")`
    before skipping a line.
  - **`IPspace of LIF` label not in key map.** The label `"IPspace of LIF"`
    was missing from `_KEY_MAP`, so ipspace was never recorded and Cluster
    LIFs were not identifiable. Added `"ipspace of lif"` to `_KEY_MAP`.
  - **`_KEY_MAP` used substring matching.** Changed all label lookups from
    `in` (substring) to `==` (exact match) to prevent partial-label
    collisions across ONTAP versions.
  - **Prefix-length netmask.** Newer ONTAP versions emit
    `Bits in the Netmask: 16` rather than a dotted netmask. Added a
    `_prefix_to_mask()` helper (prefix → dotted notation) and mapped
    `"bits in the netmask"` / `"netmask length"` in `_KEY_MAP`.
  With all of these fixed, `primary_node` and `secondary_nodes` are now
  correctly written to `reinit-config.json` after a 4e gather.
- **4e config gather — BMC prompt consumed by probe.** When connecting
  via a BMC IP, the initial probe (`direct_read_until_any` with `">"` in
  the pattern list) consumed the BMC's `>` prompt before
  `wait_for_bmc_prompt` was called, causing an immediate timeout. Fixed by
  checking whether `">"` was already in the probe output and skipping
  `wait_for_bmc_prompt` in that case.
- **Mode 3 crash — `AttributeError: 'NoneType' has no attribute 'log'`.**
  `_run_context.apply_to_globals()` at the peer-list stash step was writing
  `session_log=None` back to `_session_log` because the `RunContext`
  snapshot was taken before `_make_session_log()` was called. Fixed by
  calling `_run_context.refresh_from_globals()` immediately before
  `apply_to_globals()` so live globals — including `_session_log` — are
  preserved on the write-back.

- **`--diag` flag: diagnostic LOADER bootarg injection.** A new `--diag`
  CLI flag enables one-off LOADER bootarg injection at the LOADER stage
  (after `set-defaults`, before `saveenv`) on **all nodes** (primary and
  all peers).
  - At startup the script looks for a `bootargs.txt` or `bootargs` file in
    `configs/` (preferred) then the script directory. Each non-blank,
    non-comment line is one bootarg entry (`option_name value`). If no
    file is found the operator is prompted interactively.
  - Entry format is `option_name value` (any two-token pair; does **not**
    need to start with `bootarg.`). The `setenv` prefix must **not** be
    included — the script adds it. Missing value or `setenv` prefix
    causes an immediate hard exit.
  - After loading, all entries are printed as `setenv option value` and
    the operator must confirm before the script proceeds.
  - Bootargs are injected into `get_loader_commands()` (modes 1/2/3
    primary) and the inline peer LOADER command block in
    `_add_peer_node_thread`.
  - After each `setenv` is sent, the LOADER response is checked for error
    indicators (`%`, `Error`, `invalid`, `unknown`). On match the script
    prints the LOADER output and exits.
  - For mode 4b, the validated list is persisted to the checkpoint via
    `set_param("diag_bootargs", ...)` and restored on `--resume`,
    skipping re-prompt.
  - New module-level globals: `_diag_mode: bool`, `_diag_bootargs: list`.
  - New helper: `_load_diag_bootargs()`.

### Changed
- **Cluster node-healthy wait extended.** `_wait_for_cluster_nodes_healthy`
  default `total_timeout` increased from 600 s (10 min) to 900 s (15 min);
  default `poll_interval` increased from 120 s (2 min) to 300 s (5 min).
  Both the function-signature defaults and the explicit call site in
  `_add_peer_node_thread` were updated. Console timeout message and
  inline comment updated to reflect the new values.

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
