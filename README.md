# AFX Cluster Reinit Script

**Latest version:** `AFX_reinit.py`  
**Updated:** 6/17/2026

---

> **Disclaimer:** This script is an independent, unofficial tool and is **not sanctioned, endorsed, or provided by NetApp, Inc.** It is not an official NetApp product and is not covered by any NetApp support agreement. Use it at your own risk. NetApp bears no responsibility for any data loss, system downtime, or other consequences resulting from its use. Always validate procedures in a non-production environment before running them against production systems.

---

## Overview

Reinitalizing an ONTAP AFX cluster involves many sequential and parallel steps — including wait times between operations — that benefit greatly from automation to reduce human error and minimize hands-on time.

`AFX_reinit.py` is an automated console management script that assists NetApp field engineers and storage administrators with reinitializing NetApp AFX cluster nodes via the BMC (Baseboard Management Controller) / Service Processor (SP) console.

The script automates the following core tasks:

- Connects to the BMC/SP via SSH
- Validates BMC/SP status and existing session conflicts
- Performs a system reset or power cycle as needed
- Enters the system console and interrupts the AUTOBOOT sequence
- Executes LOADER-level boot configuration commands
- Selects the appropriate boot menu option
- Drives the ONTAP cluster setup wizard in fully automated mode
- Adds peer nodes to an existing cluster (sequentially or in parallel)
- Manages ONTAP software upgrades via rolling takeover/giveback
- Installs ONTAP via netboot
- Configures passwordless SSH access to cluster management
- Creates and saves cluster configuration backups
- Verifies BMC authentication
- Runs standalone cluster health and version checks
- Lists and cleans up stale BMC SSH sessions interactively

All session activity is captured in a timestamped log directory with a human-readable summary report and a full screen-output transcript.

---

## Prerequisites

Before running this script, ensure the following are in place:

- Python 3.6 or later installed on the client machine
- SSH access to all BMC (Baseboard Management Controller) addresses
- BMC credentials are known (username and password); SP (Service Processor) on older systems uses the same credentials
- BMC addresses are reachable from the client (port 22/TCP)
- Cluster management IP and credentials are known (for modes that interact with ONTAP)
- For config-file-driven runs: a valid `reinit-config.json` is prepared (see [Configuration File](#configuration-file))

**Terminology note:** Throughout this documentation, "BMC" (Baseboard Management Controller) refers to the out-of-band management interface. On older NetApp systems (prior to ONTAP 9.x), this component is called the "SP" (Service Processor). The terms are interchangeable — they refer to the same out-of-band console access path. When connecting via SSH or `system console`, you are connecting to the BMC/SP.

The BMC/SP must be configured and accessible over the network before running this script. Refer to the official NetApp documentation:
- [Manage SP/BMC](https://docs.netapp.com/us-en/ontap/system-admin/manage-sp-bmc-concept.html)
- [Configure SP/BMC network](https://docs.netapp.com/us-en/ontap/system-admin/configure-sp-bmc-network-concept.html)

<details>
<summary>Supported Operating Systems</summary>

The script has been tested on CentOS 7.x, Red Hat 9.x, and Ubuntu 22.04. It should work on any system that supports Python 3.6+.

| OS | Tested Versions | Package Manager |
|---|---|---|
| Red Hat Enterprise Linux (RHEL) | 7.x, 8.x, 9.x | yum / dnf |
| CentOS | 7.x, 8.x | yum / dnf |
| Fedora | Current | dnf |
| Ubuntu | 18.04, 20.04, 22.04, 24.04 | apt |
| Debian | 10, 11 | apt |
| macOS | Catalina and later | pip only |
| Windows | 10, 11 (with Python installed) | pip only |

The script automatically detects the operating system and uses the appropriate package manager (`apt`, `dnf`, or `yum`) for installing system-level dependencies. On macOS and Windows, `pip` is used exclusively.

</details>

<details>
<summary>Required Packages and Modules</summary>

**Python Modules**

| Module | Purpose | Install Method |
|---|---|---|
| `paramiko` | SSH connectivity to BMC/SP and cluster | Auto-installed by script if not present |

If `paramiko` is missing, the script detects it at startup and prompts you to install it:

```bash
# Ubuntu/Debian
sudo apt install python3-paramiko

# RHEL/CentOS/Fedora
sudo dnf install python3-paramiko
# or
sudo yum install python3-paramiko

# Fallback (all OS)
pip install paramiko
```

**Standard Library Modules (no install required)**

`subprocess`, `sys`, `os`, `time`, `re`, `getpass`, `logging`, `threading`, `signal`, `argparse`, `platform`, `socket`, `warnings`, `datetime`, `json`, `atexit`

</details>

<details>
<summary>Network Requirements</summary>

**Port Requirements**

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 22 | TCP | Client → BMC/SP | SSH connection to each node's BMC or Service Processor |
| 22 | TCP | Client → Cluster Mgmt IP | SSH connection to ONTAP cluster management (modes 4a–5g) |

**Firewall Configuration**

Ensure that port 22 (SSH) is open outbound from the client machine to all BMC/SP addresses and to the cluster management IP.

**Linux (firewalld):**
```bash
# Check firewalld status
sudo systemctl status firewalld

# Temporarily disable (re-enables on reboot)
sudo systemctl stop firewalld

# Re-enable after the procedure
sudo systemctl start firewalld
```

**Linux (iptables):**
```bash
# Check current rules
sudo iptables -L OUTPUT -n

# Allow outbound SSH if blocked
sudo iptables -A OUTPUT -p tcp --dport 22 -j ACCEPT
```

**SELinux:**

SELinux typically does not block outbound SSH. If issues occur:
```bash
# Check status
getenforce

# Temporarily set Permissive (reverts on reboot)
sudo setenforce 0

# Re-enable after procedure
sudo setenforce 1
```

> Do not permanently disable SELinux on production systems.

**Connectivity Test**

Before running the script, verify that you can reach each BMC:

```bash
# Test SSH connectivity
ssh admin@<bmc-address>

# Test port connectivity
nc -zv <bmc-address> 22
```

</details>

---

## End-to-End Reinit Time Estimates

The table below compares estimated total wall-clock time for a full end-to-end cluster reinit (primary + N−1 peer nodes) between the **old** wizard-based node-join and the **new** `cluster add-node` bulk-join, based on an observed 4-node benchmark (3094s / 51.6m total).

**Phase breakdown (observed, 4 nodes):**

| Phase | Time | Scales with nodes? |
|---|---|---|
| Early setup (SSH, LOADER, boot menu) | ~3.5m | No — constant |
| Primary node 1b (cluster init + wizard) | ~21.3m | No — constant |
| Peer parallel prep (Option 4 → cluster IP) | ~10.5m | No — all peers run simultaneously |
| Old: serial join wizard per peer | ~12m avg / peer (15m max) | Yes — ×(N−1) |
| New: `cluster add-node` bulk join | ~14m for 3 peers + ~2m per additional | Near-constant |

**Formulas:**
- **Old total:** ~35m fixed + (N−1) × ~12m serial joins
- **New total:** ~35m fixed + ~14m bulk join + ~2m per peer beyond the first 3

| Cluster Size | **Old Total** | **New Total** | **Savings** |
|---|---|---|---|
| 4 nodes (observed) | **~71m** | **~52m** | ~19m |
| 8 nodes* | **~119m (2h)** | **~57m** | ~62m |
| 16 nodes* | **~215m (3.6h)** | **~69m** | ~2.4h |
| 64 nodes* | **~791m (13.2h)** | **~175m (2.9h)** | ~10.3h |

> Based on observed 4-node run (3094s total): fixed overhead ~1496s (~25m), peer parallel prep ~630s (~10.5m), bulk join last success ~846s + ~120s health poll. Old serial join ~720s avg per peer. Observed new 4-node total was 51.6m; table shows ~52m.
>
> \* Extrapolated from 4-node observed data; not tested.

---

## Configuration File

The script accepts a JSON configuration file that pre-fills cluster and node parameters. This eliminates repeated prompts during multi-node runs and enables fully unattended automation.

### Auto-Discovery

The script automatically searches for config files in the following locations (in order):

1. `configs/reinit-config.json` (subdirectory next to the script)
2. `reinit-config.json` (same directory as the script)
3. Current working directory

The following filenames are recognized: `reinit-config.json`, `reinit_config.json`, `reinit-afx-config.json`, `reinit_afx_config.json`, `afx-reinit-config.json`, `config.json`

You can also specify the path explicitly:

```bash
python3 AFX_reinit.py --config /path/to/myconfig.json
```

### Config File Schema

```json
{
  "cluster": {
    "name":              "cluster-name",
    "clus_mgmt_address": "192.168.1.100",
    "clus_mgmt_mask":    "255.255.255.0",
    "clus_mgmt_gw":      "192.168.1.1",
    "clus_mgmt_port":    "e0M",
    "user":              "admin",
    "password":          "PASSWORDHERE",
    "dns_domains":       "example.com",
    "dns_servers":       "192.168.1.10,192.168.1.11",
    "location":          "Rack 1",
    "contact":           "admin@example.com"
  },
  "primary_node": {
    "bmc":               "192.168.2.10",
    "bmc_user":          "admin",
    "bmc_password":      "PASSWORDHERE",
    "node_mgmt_port":    "e0M",
    "node_mgmt_ip":      "192.168.2.11",
    "node_mgmt_netmask": "255.255.255.0",
    "node_mgmt_gateway": "192.168.2.1"
  },
  "secondary_nodes": [
    {
      "bmc":               "192.168.2.20",
      "bmc_user":          "admin",
      "bmc_password":      "PASSWORDHERE",
      "node_mgmt_port":    "e0M",
      "node_mgmt_ip":      "192.168.2.21",
      "node_mgmt_netmask": "255.255.255.0",
      "node_mgmt_gateway": "192.168.2.1"
    }
  ]
}
```

### Field Behavior

| Field value in JSON | Runtime behavior |
|---|---|
| Field omitted (key not present) | Script prompts the operator at runtime |
| Field set to `""` (empty string) | Used as-is with no prompt. For passwords this means "no password". |
| Field set to a non-empty value | Used directly, no prompt |

Print a ready-to-edit example config at any time:

```bash
python3 AFX_reinit.py --config-example
```

The `primary_node` is the node used to initialize the cluster (options 1a/1b/3). `secondary_nodes` are nodes added to the cluster (options 2a/2b and the node-add phase of option 3). The primary node must not be included in `secondary_nodes`.

---

## Operation Modes

The script presents a menu at startup. Enter the number corresponding to the desired mode.

| Mode | Short Name | Description |
|---|---|---|
| **1a** | Initialize First Node (interactive) | Boots to LOADER, sets `destroy-all-storage-pods` flag, selects boot menu option 9 (Clean System Configuration). Prompts the operator for all cluster setup wizard inputs. |
| **1b** | Initialize First Node (automated) | Same as 1a, but drives the full ONTAP cluster setup wizard automatically using values from config file or prompts. |
| **2a** | Add Node to Cluster (interactive) | Boots to LOADER, selects boot menu option 4 (Initialize and configure system). Operator completes the node-join wizard. In multi-node runs, supports numbered omit selection and auto-skips nodes already in cluster. Per-node credential collection can use password groups, and BMC auth attempts include silent fallback (including blank password). |
| **2b** | Add Node to Cluster (automated) | Same as 2a, but drives the node-join wizard automatically. Supports adding multiple secondary nodes in parallel, numbered omit selection, and auto-skips nodes already in cluster. In this flow, "primary BMC" is used as the default credential context (use `PRIMARY` to reuse that password; blank means an actual blank password), not as a unique controller after parallel add starts. Per-node credential collection can use password groups, and BMC auth attempts include silent fallback (including blank password). |
| **2c** | Resume Node Additions | Resumes interrupted node-join operations from the last successful checkpoint. Use when a previous mode 2b or mode 3 run was interrupted before all secondary nodes completed. Run `--checkpoint-status` to inspect the checkpoint state before resuming. |
| **3** | End-to-End Auto Reinit | Runs mode 1b on the primary node, then runs mode 2b on all secondary nodes in parallel. Fully unattended with a config file. Peer-credential collection supports password groups, and peer BMC connect/reconnect paths use silent fallback credentials (including blank password). **Option 3 is reinit-only and assumes ONTAP is already at the target version; use 4b or 4c for image installs.** |
| **4a** | ONTAP Upgrade | Performs a rolling upgrade of both nodes via automated takeover, software update, and giveback sequence. See [Why 4a uses the BMC](#why-4a-uses-the-bmc). |
| **4b** | Netboot Install + Optional Reinit | Runs netboot image install, then can continue into reinit flow (1a/1b/3) when selected. |
| **4c** | Netboot Install Only | Runs the same netboot image install path as 4b, then stops before reinit, cluster create, or node add steps. |
| **5a** | License Install | Installs ONTAP licenses on an existing cluster. |
| **5b** | SSH Key Setup | Configures passwordless SSH from the script host to the cluster management interface. |
| **5c** | Config Backup | Connects to the cluster and captures its current configuration (name, IPs, licenses, nodes) to a JSON file. Can also build a config file manually from user prompts. Gather/build paths that connect to an existing cluster also write `configs/cluster_IP.json` for node-add ordering reuse. |
| **5d** | BMC Auth Verify | Tests BMC SSH authentication for configured nodes and reports pass/fail. Shows a numbered target list, supports all-or-subset selection, and a rerun can re-open the target picker to test a different selection. |
| **5f** | Check Node Status | Connects to each BMC and reports whether nodes are at LOADER, ONTAP shell, login prompt, boot menu, or unknown state. |
| **5g** | Cluster Health Check | Connects to the cluster management LIF via SSH and checks health/version. |
| **5h** | Stale BMC Session Cleanup | Interactive tool to list and clean up stale SSH/SOL connections to BMC/SP addresses. SSH diagnostics one-IP targeting uses a numbered, labeled config-IP picker (BMC/cluster mgmt/node mgmt) with a custom-IP option. Includes a dedicated known_hosts reset action (`ssh-keygen -R <BMC IP>`). |
| **5i** | Backup LOADER Environment Variables | Backs up current LOADER bootenv variables to a timestamped JSON file (e.g., `loader_env_backup_YYYYMMDD_HHMMSS.json`) for comparison and troubleshooting. Part of LOADER environment utilities (experimental). |
| **5j** | Compare LOADER Environment | Compares current LOADER bootenv variables against NetApp defaults and displays a diff showing customizations and deviations. Helps identify bootenv changes and troubleshoot configuration issues (experimental). |
| **5k** | Check Boot DNA | Loads target IPs from JSON config and shows a numbered selector: **1)** all discovered BMC IPs, **2)** cluster management IP, **3)** custom IP. It evaluates each target's runtime state (**At LOADER** or **At cluster shell**), runs the matching DNA command path, and reports `bootarg.init.dna` with a per-target state/value summary when multiple nodes are checked. |
| **5l** | Build Cluster IP Manifest | Connects to cluster management and runs cluster-role interface queries to write `configs/cluster_IP.json`. Stores one cluster IP per node (the first seen per node), preserving file order so 2a/2b/3/4b can reuse this manifest for ordered `cluster add-node -cluster-ips` arguments. **Status: EXPERIMENTAL/IN PROGRESS.** |
| **5z** | Reset to LOADER | Connects to configured BMC addresses in parallel, issues a system reset on each selected node, enters the system console, and sends Ctrl+C to interrupt AUTOBOOT. Shows a numbered target list and supports running against all entries or a comma-separated subset of selected numbers. The script exits when every selected node has reached the `LOADER>` prompt (or reports failure per node). Useful for staging nodes before a manual reinit or netboot run. |

> **Warning:** Options 1a and 1b destroy all storage on the target node and reinitialize the cluster. If a cluster already exists, use option 2 instead.

---

### Why 4a uses the BMC

The upgrade workflow drives the cluster through the BMC console rather
than a plain SSH session to a cluster management LIF. The BMC is the
only path that survives every phase of the upgrade:

1. **Console session is reboot / takeover / giveback proof.** `system console`
   over the BMC is serial-over-LAN, so the session stays attached to a
   node's CPU even when its management LIF migrates to the partner, the
   node reboots into the new image, or it stops at the `LOADER>` prompt.
   An SSH session to a cluster-mgmt LIF would drop the instant the LIF
   moved or the hosting node rebooted — exactly when visibility matters
   most.
2. **Visibility into LOADER and panics.** If a new image fails to boot,
   the LOADER (or panic) prompt only appears on the console. Network
   management is gone at that point.
3. **Cluster login bootstrap.** When the script first attaches, the
   cluster LIFs may be unreachable (pre-reinit, post-reboot, mid-
   takeover). The BMC always answers, and the cluster shell can be
   reached through `system console` without depending on cluster
   networking being healthy.
4. **Free credential reuse.** The reinit workflow already collected BMC
   credentials and stored them in the reinit config file. 4a picks those
   up from the file via a numbered picker and reuses the same
   user/password for the cluster login, eliminating extra prompts in the
   common case.

The parallel image-install path added in this version is an
optimization layered on top: once the cluster shell is up and the
node-management LIFs are reachable, the actual `system image update`
commands are plain cluster CLI calls that parallelize well over a
direct SSH to each node's management IP (pulled from the reinit
config). The BMC remains the lifeline for login,
`promoted-dev-update`, and the rolling takeover/giveback steps where
the cluster LIF is in flux.

---

## Checkpoint & Resume (modes 4b and 3)

Mode **4b** (including the end-to-end variant **4b + reinit mode 3**)
and the standalone end-to-end mode **3** persist progress to a
checkpoint file so an interrupted run — Ctrl+C, network blip, BMC
banner stall, power loss on the jump host — can be resumed without
re-running destructive steps.

> **Tip:** Run `python3 AFX_reinit.py --checkpoint-status` at any time to inspect the saved checkpoint — file path, run mode, age, BMC IPs, and completed phases — without modifying or resuming it.
> **Note:** Checkpointing is experimental and still a work in progress.

### Where the checkpoint lives

A single JSON file named **`afx_checkpoint.json`** is written to the same
directory as the script (next to `AFX_reinit.py`). Checkpoints older than
**72 hours** are ignored on load.

### How to inspect it

Use the dedicated CLI flag — no need to open the JSON by hand:

```bash
python3 AFX_reinit.py --checkpoint-status
```

This prints the absolute checkpoint path, the run mode (e.g. `4b-3`),
created/updated timestamps, age in minutes, log directory, config path,
BMC IPs, every completed global phase, and every per-node phase keyed by
BMC IP — then exits without modifying the file.

The same summary is also printed automatically at startup whenever a
valid checkpoint is found, immediately before the resume / discard
prompt.

### How to resume

```bash
python3 AFX_reinit.py --resume
```

On startup the script loads `afx_checkpoint.json`, shows the summary,
and resumes mode 4b from the first unfinished phase. Completed work is
skipped:

- All BMC IPs marked `install_done` → Steps 2–6a (SSH / reset / netboot /
  install / boot menu option 6) are skipped; the run jumps straight to
  Step 6b (reconnect to LOADER and boot ONTAP).
- Peers marked `peer_joined` (mode 3 only) are skipped during the
  parallel peer auto-add phase.
- `cluster_formed`, `primary_setup_done`, and `option3_complete` gate
  the cluster-setup wizard, license/SSH steps, and the finalize banner.

If `option3_complete` or `primary_setup_done` is set from a prior run,
the resume prompt warns that re-running will destroy the existing
cluster and asks for explicit confirmation.

### Phase glossary

| Phase | Scope | Set when |
|---|---|---|
| `install_done` | per-node | Option 6 (Update flash from backup config) succeeds and the node reaches the post-install `login:` prompt. |
| `reinit_loader` | per-node | Reconnect-to-LOADER succeeds and `boot_ontap menu` has been sent. |
| `primary_bootmenu_done` | global | The primary node clears the ONTAP boot menu (option 9 for mode 1b/3, option 4 for mode 2). Cluster setup wizard is about to begin. |
| `cluster_formed` | global | `cluster create` succeeds on the primary node and the prompt reaches `::>`. |
| `primary_setup_done` | global | The primary cluster-setup wizard returns successfully. |
| `peer_option4_done` | per-peer (mode 3) | A peer clears boot menu option 4, finishes format, and reaches the join barrier. Recorded once per peer so the option-4 / format work can be reasoned about on resume. |
| `peer_joined` | per-peer (mode 3) | A peer completes the join wizard and the primary's `cluster show` confirms it. |
| `option3_complete` | global | The end-to-end mode-3 finalize banner has been printed. The checkpoint file is then deleted. |

### Manual checkpoint snapshots

At any point during a live run you can force an immediate checkpoint snapshot — a timestamped copy of the current checkpoint state — without stopping the script.

**Method 1 — sentinel file (any OS):**

```bash
# Create the trigger file next to AFX_reinit.py
touch .afx_checkpoint_now
```

The script detects the file at the next internal poll, writes a snapshot to `checkpoints/afx_checkpoint_manual_<YYYYMMDD_HHMMSS>.json`, then removes the trigger file.

**Method 2 — Unix signal (Linux / macOS):**

```bash
# The script prints the PID and signal command at startup, e.g.:
#   signal checkpoint: kill -URG <pid>
kill -URG <pid>
```

`SIGURG` triggers the same snapshot write without touching the filesystem.

The script prints the saved path on screen:

```
💾 Manual checkpoint saved (operator): checkpoints/afx_checkpoint_manual_20260613_151200.json
```

Manual snapshot files are separate from the live `afx_checkpoint.json` that `--resume` uses; they are kept for audit/diagnostic purposes and are not loaded automatically.

### Clearing the checkpoint

The script removes `afx_checkpoint.json` automatically on successful
completion of mode 4b. To discard a stale checkpoint manually, delete
the file or answer **no** at the resume prompt.

---

## Pause & Resume (runtime control)

During any automated run (modes 1b, 2b, 3, 4a, 4b) you can pause automation in-place without killing the script. The script freezes at the next safe yield point (typically between boot stages or before issuing a cluster command), then resumes exactly where it left off when the pause is lifted.

At startup the script prints the pause controls for the current run, for example:

```
⏯️  Runtime pause control:
   create file: /scripts/AFX/.afx_pause
   remove file: resume automation
   signal toggle: kill -USR1 12345
   signal resume: kill -USR2 12345

💾 Runtime manual checkpoint:
   create file: /scripts/AFX/.afx_checkpoint_now
   signal checkpoint: kill -URG 12345
```

### How to pause

**Method 1 — sentinel file (any OS):**

```bash
# Pause: create the file
touch .afx_pause

# Resume: remove the file
rm .afx_pause
```

**Method 2 — Unix signals (Linux / macOS):**

```bash
kill -USR1 <pid>   # toggle pause on/off
kill -USR2 <pid>   # force resume (clear pause)
```

While paused the script prints:

```
⏸️  Pause requested (boot menu wait). Automation and auto-reconnect are paused.
   Remove pause file to resume: /scripts/AFX/.afx_pause
```

When the pause file is removed (or `USR2` sent) the script immediately resumes:

```
▶️  Pause cleared. Resuming automation.
```

### When to use pause

| Situation | Action |
|---|---|
| Unexpected console state — you want to inspect before the script advances | Pause, investigate, remove the pause file |
| Long-running boot wait — you want to snapshot state before a risky phase | Pause + create `.afx_checkpoint_now`, then resume |
| Step-debug a wizard phase without killing the run | Pause between phases |

> **Note:** Pause does not affect already-running background threads (parallel peer adds, parallel image installs). It freezes the *coordination* layer — new phases will not start, reconnects will be deferred — but threads that are actively mid-operation finish their current step.

---

## Runtime Control Signals (Advanced)

While AFX_reinit.py is running, you can control execution using standard Unix signals (Linux/macOS only) or by sending SIGINT (Ctrl+C on all platforms). These are useful for long-running cluster initialization workflows that may need graceful shutdown, temporary pause, or manual checkpointing.

| Signal | Purpose | Command | Use Case |
|--------|---------|---------|----------|
| SIGHUP | Graceful shutdown | `kill -HUP <pid>` | Terminal disconnected; log is flushed cleanly and the run pauses without losing progress. Useful with `nohup` or detached terminals. |
| SIGUSR1 | Toggle pause mode | `kill -USR1 <pid>` | Suspend automation; inspect system state. Send again to resume. Automation freezes at the next safe yield point. |
| SIGUSR2 | Force resume | `kill -USR2 <pid>` | Resume from pause without waiting. Clears any active pause immediately. |
| SIGURG | Manual checkpoint | `kill -URG <pid>` | Force an immediate checkpoint snapshot (timestamped copy) without stopping the script. Useful before a risky phase. |
| SIGINT | Graceful exit | `Ctrl+C` | Exit automation cleanly with full cleanup; preserves logs and checkpoint state. On Windows, `Ctrl+C` is the only signal-like control available. |

**Example: long-running mode 3 (end-to-end reinit) with supervision:**

```bash
# Start in background (or inside screen for terminal persistence)
python3 AFX_reinit.py --reinit --config configs/reinit-config.json --bg &
SCRIPT_PID=$!

# Discover the PID if needed
ps aux | grep AFX_reinit | grep -v grep

# Pause after 30 minutes to inspect cluster state (automation freezes at safe point)
kill -USR1 $SCRIPT_PID

# Inspect the cluster manually, then resume
kill -USR2 $SCRIPT_PID

# Or create a checkpoint snapshot before a risky phase
kill -URG $SCRIPT_PID

# Exit cleanly if needed (Ctrl+C also works)
kill -TERM $SCRIPT_PID
```

**Note:** 
- On Windows, signals are not supported. Use `Ctrl+C` to exit cleanly or the pause file method (`.afx_pause`) documented in **Pause & Resume** above.
- The script prints signal commands at startup for easy reference.
- `SIGHUP` is automatically triggered when running with `--bg` flag and the SSH session closes.

---

## LOADER Commands Reference

| Mode | LOADER Commands |
|---|---|
| 1a / 1b | `set-defaults`, `setenv bootarg.destroy.all.storage.pods true`, `saveenv`, `boot_ontap menu` → Option 9 |
| 2a / 2b / 2c | `set-defaults`, `saveenv`, `boot_ontap menu` → Option 4 |
| 4b | `set-defaults`, `setenv AUTOBOOT false`, `saveenv`, netboot sequence |

---

## Command-Line Reference

```
python3 AFX_reinit.py [OPTIONS]
```

| Option | Short | Description |
|---|---|---|
| `--config PATH` | `-c PATH` | Path to a JSON config file. If omitted, the script auto-discovers config files or prompts for all values. |
| `--config-example` | | Print an annotated example config file and exit. |
| `--debug` | `-d` | Enable debug mode: print all raw console I/O to the screen. Also enables verbose Paramiko SSH logging. |
| `--bg` | | Background mode: handle SIGHUP so the log is closed cleanly when the terminal closes. Use with `nohup` or `screen`. |
| `--screen` | | Re-launch the script inside a detached GNU screen session. Keeps the run alive if your SSH connection drops or times out. Implies `--bg`. Use `screen -r afx-reinit` to reattach. No-op if already running inside screen. |
| `--resume` | | Mode 4b only. Resume the previous 4b run from its saved checkpoint (`afx_checkpoint.json`). Skips phases already completed so you do not have to restart from scratch after a failure or Ctrl+C. See **Checkpoint & Resume** below. |
| `--checkpoint-status` | | Print a summary of the saved checkpoint (`afx_checkpoint.json`) — file path, run mode, age, BMC IPs, completed global phases, completed per-node phases — then exit. Does not modify the checkpoint file. |
| `--last-status` | | Read and display the summary file from the most recent AFX_reinit run, then exit. Useful for quickly checking the result of a previous job without scrolling through the full log file. |
| `--auto-clear-stale-bmc` | | On banner-timeout retries, scan for `ESTABLISHED` TCP sockets to each BMC's port 22 owned by other Python processes on this host and `SIGTERM` them. The "always-on" cleanup (close own SSH clients + `ipmitool sol deactivate`) runs regardless of this flag. See [BMC SSH Stale Session Diagnostics](#bmc-ssh-stale-session-diagnostics). |
| `--diag` | | Enable diagnostic bootarg injection. Loads `bootargs.txt` or `bootargs` from `configs/` or the script directory (one `option_name value` entry per line; lines starting with `#` are comments) or prompts interactively. After loading, all entries are printed and must be confirmed before proceeding. Bootargs are set via `setenv` after `set-defaults` and before `saveenv` at the LOADER stage on all nodes. See [Diagnostic Bootargs (`--diag`)](#diagnostic-bootargs---diag). |
| `--help` / `-h` | | Show a short man page about the script's options. |
| `--version` | | Print script version and last update timestamp, then exit. |

**Startup command completion:** If `argcomplete` is installed and your shell is configured for it, Tab can complete the startup flags above (for example `--reinit`, `--config`, `--screen`) before the script starts. See the `argcomplete` docs for the shell hook setup.

### Mode Shortcut Flags

These flags bypass the interactive menu and launch directly into the specified mode. They can be combined with `--config`, `--debug`, `--screen`, and other flags.

| Flag | Mode | Description |
|---|---|---|
| `--first-node` | 1b | Initialize the first node and set up the cluster automatically. |
| `--add-nodes` | 2b | Add node(s) to an existing cluster automatically. |
| `--reinit` | 3 | End-to-end automated reinit: 1b on primary + parallel node adds. Assumes ONTAP is already at the desired version (install via 4b/4c separately). |
| `--netboot-install` | 4b | Netboot and install ONTAP. |
| `--add-lic` | 5a | Install license file only. |
| `--passwordless` | 5b | Configure passwordless SSH to cluster management. |
| `--backup` | 5c | Create a backup cluster configuration file. |
| `--verify` | 5d | Verify BMC authentication for all configured nodes. |
| `--loader` | 5z | Reset all nodes to the LOADER prompt in parallel via BMC. |

**Examples:**

```bash
# Full unattended reinit using a config file
python3 AFX_reinit.py --reinit --config configs/reinit-config.json --screen

# Netboot all nodes then reinit (inside screen, detached)
python3 AFX_reinit.py --netboot-install --screen --config configs/reinit-config.json

# Back up the current cluster config
python3 AFX_reinit.py --backup

# Verify BMC credentials before starting a reinit
python3 AFX_reinit.py --verify --config configs/reinit-config.json

# Reset all nodes to LOADER prompt in parallel
python3 AFX_reinit.py --loader --config configs/reinit-config.json

# Add a license without running a reinit
python3 AFX_reinit.py --add-lic --config configs/reinit-config.json

# Check the result of the most recent run
python3 AFX_reinit.py --last-status
```

### Interactive Features

> ✨ **Tip:** On Linux/macOS/Unix, press **Tab** when entering file paths to auto-complete file and directory names. Start typing a path like `/scripts/ONTAP/` and press Tab to see matching options. This can save significant time when selecting large ONTAP images or config files. See details below.

**Path Tab Completion:** When the script prompts for a file path or URL (e.g., `Path or URL: /scripts/ONTAP`), you can press **Tab** to auto-complete matching paths from the filesystem. This feature is available on Linux, macOS, and Unix systems that have Python's `readline` module. On each Tab press:
- The script lists matching files and directories in the current directory
- Directory names are suffixed with `/` to indicate you can continue typing
- Partial names are completed to the longest unambiguous match

This works for:
- Config file paths (`--config` or interactive prompts)
- ONTAP image paths (mode 4b netboot)
- Bootargs files (when using `--diag`)
- License file paths (mode 5a)
- Any other file/URL input

Example:
```bash
Path or URL: /scr[TAB]
→ /scripts/
Path or URL: /scripts/O[TAB]
→ /scripts/ONTAP/
Path or URL: /scripts/ONTAP/ONTAP[TAB]
→ /scripts/ONTAP/ONTAP-9.15.1.img
```

---

## Step-by-Step Instructions

### Step 1: Download and Place the Script

Clone the repository onto the client machine that has network access to all BMC/SP addresses and the cluster management IP:

```bash
git clone https://github.com/whyistheinternetbroken/AFX.git
cd AFX
```

To pull the latest updates later:

```bash
git pull
```

Alternatively, download `AFX_reinit.py` directly and place it in a dedicated directory:

```bash
mkdir ~/afx-reinit
cp AFX_reinit.py ~/afx-reinit/
cd ~/afx-reinit
```

### Step 2: (Optional) Create a Config File

For automated or multi-node runs, create a `reinit-config.json`. There are three ways:

> **Tip:** If no config file or `BMC_IP.json` is found when you start modes 1 (initialize) or 3 (full reinit), the script will automatically ask whether you'd like to generate one from an existing cluster before proceeding — choosing **Y** launches option 5c inline.

**Option A — Back up from a live cluster (recommended):** If the cluster is currently running, use `--backup` to capture its configuration automatically:

```bash
python3 AFX_reinit.py --backup
# Follow the prompts to connect to the cluster; config is saved to configs/reinit-config.json
```

**Option B — Generate a blank template and fill it in manually:**

```bash
python3 AFX_reinit.py --config-example > configs/reinit-config.json
vi configs/reinit-config.json
```

**Option C — Build interactively:** Run the script without a config file and enter values at the prompts; save the resulting config when offered at the end of the run.

### Step 3: Run the Script

```bash
# Standard interactive run
python3 AFX_reinit.py

# With explicit config file
python3 AFX_reinit.py --config configs/reinit-config.json

# With debug output
python3 AFX_reinit.py --debug

# Auto-launch in screen (recommended for remote/SSH sessions)
python3 AFX_reinit.py --screen --config configs/reinit-config.json
# Reattach later with: screen -r afx-reinit

# In background via nohup (alternative to --screen)
nohup python3 AFX_reinit.py --bg --config configs/reinit-config.json > nohup.out 2>&1 &
```

What happens at startup:
- The script checks for required Python modules (`paramiko`); installs if missing.
- If a config file is found, you are prompted to use it or enter values manually.
- A session log directory is created under `logs/YYYYMMDD_HHMMSS/`.

### Step 4: Select an Operation Mode

The script presents a numbered menu. Enter the number corresponding to the desired mode. See [Operation Modes](#operation-modes) for a full description of each option.

### Step 5: Enter Credentials (if not in config file)

If no config file was loaded (or if fields were left blank), the script prompts for:

- BMC/SP hostname or IP address
- BMC/SP username and password
- Cluster management IP, username, and password (where applicable)
- Node management port, IP, netmask, gateway

### Step 6: BMC Connection and Validation

The script establishes an SSH connection to the BMC/SP and waits for the initial prompt. If an existing session is detected:

- **Interactive mode:** you are asked whether to disconnect the existing session.
- **Automated mode (modes 1b, 2b, 3):** the existing session is automatically disconnected.

### Step 7: System Reset

The script issues a system reset (or power cycle) command to the controller. It then waits for the console to become active. The script watches for expected output patterns at each stage. If a pattern is not seen within the timeout window, an error is logged and the script either retries or prompts the operator.

### Step 8: AUTOBOOT Interruption

Once the LOADER prompt appears, the script:

1. Sends the appropriate `set-defaults` and `setenv` commands
2. Calls `saveenv` to persist the settings
3. Issues `boot_ontap menu` to display the boot menu
4. Selects the appropriate boot menu option (option 4 or option 9)

### Step 9: Post-Boot Wizard

Depending on the mode:

- **1a (interactive):** The script provides a live terminal passthrough. The operator answers wizard questions manually.
- **1b / 2b / 3 (automated):** The script drives the wizard using config file values or pre-supplied prompts. For 1b netboot-before-reinit flows, the autopilot banner is shown after package selection (right before HTTP server/start of unattended phases). Option 3 does not run install-first flows; run 4b/4c for ONTAP installs.

### Step 10: Multi-Node Parallel Operations (modes 2b and 3)

In mode 2b and mode 3, secondary nodes are processed in parallel worker threads. The script monitors each thread and aggregates results. Each node reports independently to the session log.
The primary BMC acts as the initial/default credential source for this phase; once worker threads start, each node uses its resolved per-node BMC credentials and is processed independently.

### Step 11: Exit and Review Logs

When the script completes, it prints the path to the log directory and a brief summary. Review the summary file for timing, warnings, and errors.

---

## Session Logging

All output is captured in a timestamped log directory:

```
logs/
  YYYYMMDD_HHMMSS/
    bmc_session_<timestamp>.log    ← full raw console transcript (BMC/ONTAP I/O)
    screen_output_<timestamp>.log  ← complete transcript of what was printed on screen
    summary_<timestamp>.log        ← human-readable timing and outcome summary
```

The `logs/` directory is created in the same folder as the script.

**`screen_output_*.log`** captures everything that would appear on the operator's terminal — menus, prompts, status lines, and milestone messages — in clean plain text (ANSI escape codes stripped). It is the easiest file to review after a run to see exactly what happened from a user's perspective.

**`bmc_session_*.log`** contains the raw BMC/ONTAP console I/O and all structured log entries with timestamps.

### Summary File Format

The summary file contains:

- **Result:** PASS, PASS (with warnings), or FAIL
- **Phase Timing:** duration of each named phase (e.g., "BMC Connect", "LOADER", "Wizard", "Auto Join"). Includes:
  - **Indented sub-rows** for phases that support per-node breakdown (e.g., `[node] image download` and `[node] image install` under the netboot install phase).
  - **`Pause wait (xN)` row** showing aggregate operator-pause time (total seconds held, pause count, and a `longest single pause` sub-line with context label) when the run was paused at least once.
- **Step Timing:** duration of individual steps within each phase
- **Warnings (N):** grouped by source log file; each block starts with the log file path, followed by timestamped warning messages
- **Errors (N):** timestamp and message for each error logged during the run

Example summary:

```
==================================================
SESSION SUMMARY — Mode 3: End-to-End Reinit (automated)
Result : PASS
==================================================

Phase Timing
  BMC Connect             :   3.2s
  System Reset            :  12.4s
  LOADER                  :  18.1s
  4b – Netboot Install    : 412.3s
    [node-01] image download :  85.1s
    [node-01] image install  : 201.4s
    [node-02] image download :  83.7s
    [node-02] image install  : 198.6s
  Wizard                  : 142.7s
  Auto Join               : 814.5s
  Pause wait (x2)         : 120.0s
     - longest single pause: 90.0s (1.5m) context: boot menu wait
  Total                   : 1523.2s

Step Timing
  wait_bmc_prompt     :   3.2s
  send_reset          :   0.1s
  wait_autoboot       :  12.3s
  ...

Warnings (0)
  (none)

Errors (0)
  (none)
==================================================
```

---

## Debug Mode

Enable with `--debug` or `-d`.

In debug mode:

- All raw console I/O (BMC and ONTAP) is printed directly to the terminal in addition to being written to the log file.
- Python logging is set to DEBUG level, showing verbose Paramiko SSH negotiation and channel activity.

Useful for diagnosing unexpected hangs, mismatched prompt patterns, or SSH authentication issues.

```bash
python3 AFX_reinit.py --debug
```

---

## Screen Mode

Enable with `--screen`.

When `--screen` is specified the script checks whether it is already running inside a GNU screen session (via the `STY` environment variable). If not, it:

1. Verifies that `screen` is installed (exits with install instructions if missing)
2. Strips `--screen` from the argument list to prevent recursion
3. Appends `--bg` so the log is flushed cleanly on detach
4. Spawns: `screen -dmS afx-reinit python3 AFX_reinit.py --bg [other args]`
5. Prints the reattach command and exits the outer process

The script then runs entirely inside the screen session. If your SSH connection drops, the run continues uninterrupted. Reconnect to the host and reattach:

```bash
# Launch in screen
python3 AFX_reinit.py --screen --config configs/reinit-config.json

# Reattach after reconnecting
screen -r afx-reinit

# List active sessions
screen -ls
```

> - `--screen` implies `--bg`. You do not need to specify both flags.
> - `--screen` is a no-op if you are already inside a screen (or tmux) session — the script detects this and continues normally without spawning a child session.
> - GNU screen must be installed on the client machine. If it is missing, the script will print install instructions and exit cleanly.
> - `--screen` is available on Linux and macOS only. On Windows, use WSL or a Linux jump host for equivalent functionality.

---

## Background Mode

Enable with `--bg`.

Registers a SIGHUP handler so the session log is flushed and closed cleanly when the controlling terminal disconnects. Use this when running the script via `nohup`, `screen`, or `tmux`.

```bash
# Using nohup
nohup python3 AFX_reinit.py --bg --config configs/reinit-config.json > nohup.out 2>&1 &

# Manually launching inside screen
screen -S afx-reinit python3 AFX_reinit.py --bg --config configs/reinit-config.json
# Detach with Ctrl+A, D
# Reattach with: screen -r afx-reinit
```

> For the most convenient experience with screen, use `--screen` instead — it handles session creation automatically. See [Screen Mode](#screen-mode).

> Note: SIGHUP is not supported on Windows. The `--bg` flag is accepted but has no effect on that platform.

---

## BMC SSH Stale Session Diagnostics

BMC controllers limit the number of concurrent SSH sessions (typically four). If a previous run crashed or was killed without closing its connections, those "ghost" sessions keep slots occupied and cause new connection attempts to fail with a banner-timeout error.

The script has a multi-layer automatic and interactive system to detect and clear these stale sessions.

### Automatic cleanup (always on)

On every banner-retry attempt (up to 5 retries, 60 s apart), the script automatically:

1. **Diagnoses** — scans local TCP state and prints a report of which processes hold open connections to the BMC's port 22 (stale Python PIDs, operator SSH sessions, etc.)
2. **Closes own clients** — drops any `paramiko` SSH clients that *this* process still holds open to the affected BMC
3. **Runs `ipmitool sol deactivate`** — if `ipmitool` is on `PATH` and BMC credentials are available, deactivates any stuck SOL (Serial-over-LAN) session; a hung SOL session is one of the most common BMC session-slot consumers

### `--auto-clear-stale-bmc` (optional, more aggressive)

```bash
python3 AFX_reinit.py --auto-clear-stale-bmc
```

When this flag is set, the banner-retry cleanup additionally:

- Scans for `ESTABLISHED` TCP sockets to `<bmc>:22` owned by **other Python processes** on this host (prior AFX_reinit runs that died without releasing connections)
- Sends `SIGTERM` to those PIDs to force-close their connections

> **Caution:** This can terminate a concurrent script invocation run by another operator on the same jump host. Use it only when you are certain no other active run shares the same host.

### Interactive cleanup (mode 5d)

When **mode 5d** (BMC Auth Verify) reports failures and you decline to re-enter addresses, the script offers an interactive diagnostic and cleanup pass:

```
  🔍 Diagnosing SSH state for 192.168.2.10...

  Attempt to clear stale SSH sessions for these BMC(s)?
    • run 'ssh-keygen -R <BMC IP>' to clear known_hosts entries
    • drop in-process SSH clients we still hold
    • run 'ipmitool sol deactivate' (if ipmitool is installed)
    • SIGTERM other-python PIDs only if --auto-clear-stale-bmc was given (currently: OFF)
  Proceed? [y/N]:
```

Answering `y` runs the same cleanup pass as the automatic retry path, then instructs you to re-run the script.

### Example diagnostic output

During a banner-timeout retry:

```
⚠️  [node01] BMC SSH banner not received from 192.168.2.10 (BMC may still be starting up). Waiting 60s and retrying (up to 5 retries)...
  🔍 [192.168.2.10] stale-session diagnosis:
      In-process SSH clients (this script): 1
      Other python PIDs with open sockets to BMC:22: 2
          - pid=12345 (python3)
          - pid=12346 (python3)
      💡 Re-run with --auto-clear-stale-bmc to SIGTERM these prior runs automatically.
  🧹 [192.168.2.10] ipmitool: SOL session deactivated.
```

If no stale local sockets are found but the banner timeout persists, the script prints:

```
  🔍 [192.168.2.10] stale-session diagnosis: no stale local SSH sockets to BMC:22 found.
      BMC slot pool is likely starved server-side (try ipmitool sol deactivate).
```

In that case the BMC's session pool is likely full from sessions opened by other hosts or devices. Manually running `ipmitool sol deactivate` from the jump host (or rebooting the BMC) may be necessary.

### `ipmitool` installation

`ipmitool` is optional. If it is not installed, the SOL-deactivate step is silently skipped.

```bash
# Ubuntu/Debian
sudo apt install ipmitool

# RHEL/CentOS/Fedora
sudo dnf install ipmitool
```

---

## Known Issues and Gotchas

- **BMC session timeout:** Some BMC firmware versions disconnect idle sessions after 5–10 minutes. If the script appears to hang waiting for the LOADER prompt after a long delay, try re-running with a fresh BMC session.

- **Boot menu timing:** The window for interrupting AUTOBOOT is narrow. The script attempts the interrupt character as soon as it detects the AUTOBOOT countdown. If the system boots fully before the interrupt is sent, the script will report an error. Reset the node and re-run.

- **ONTAP wizard timeouts:** The ONTAP cluster setup wizard occasionally pauses for DNS lookups or license validation. The script uses generous timeouts for these steps but may time out on very slow networks. Run with `--debug` to observe wizard progress in real time.

- **Parallel node adds:** In modes 2b and 3, all secondary nodes are started simultaneously. If one node fails, the others continue running. Check the summary log for per-node results.

- **Config file and empty string fields:** Setting a password field to `""` in the config file means the script will send an empty password (no prompt). This is intentional for BMCs that use passthrough credentials. Do not set `""` for fields that require real values.

- **Windows:** The `--bg` SIGHUP handler is a no-op on Windows (SIGHUP is not supported). The script still runs correctly; the warning can be ignored. The `--screen` flag is also unavailable on Windows (GNU screen is Linux/macOS only); use WSL or a Linux jump host for long-running sessions.

---

## Troubleshooting

### Pause a live run for manual BMC console commands

See [Pause & Resume (runtime control)](#pause--resume-runtime-control) for full details including signal-based controls and when to use each method.

```bash
# Create/remove in the same directory as AFX_reinit.py
touch .afx_pause    # pause automation
rm -f .afx_pause    # resume automation
```

### Create a manual checkpoint mid-run

See [Manual checkpoint snapshots](#manual-checkpoint-snapshots) for full details including signal-based triggering.

```bash
# Create request file in the same directory as AFX_reinit.py
touch .afx_checkpoint_now
```

The script writes a timestamped snapshot under `checkpoints/` as:
`afx_checkpoint_manual_YYYYMMDD_HHMMSS.json`.


### BMC SSH banner timeout (session pool full)

If the script repeatedly fails to connect with a banner-timeout error even after a node has fully booted:

- The BMC's SSH session pool is likely exhausted by stale connections from prior runs
- Watch the automatic diagnostic output printed before each retry — it lists which local PIDs hold open sockets to the BMC
- If stale Python PIDs are listed, re-run with `--auto-clear-stale-bmc` to terminate them automatically
- If no local stale sockets are found, the sessions may be held by other hosts; run `ipmitool sol deactivate` manually against the BMC, or reboot the BMC via its web interface
- See [BMC SSH Stale Session Diagnostics](#bmc-ssh-stale-session-diagnostics) for the full explanation

### `ModuleNotFoundError: No module named 'paramiko'`

The script should auto-detect this and prompt to install. If it does not:

```bash
pip install paramiko
# or
sudo apt install python3-paramiko   # Ubuntu/Debian
sudo dnf install python3-paramiko   # RHEL/Fedora
```

### "Connection refused" or "SSH timeout" connecting to BMC

- Verify the BMC address is correct and reachable: `ping <bmc-address>`
- Verify port 22 is open: `nc -zv <bmc-address> 22`
- Verify firewall rules on the client (see [Network Requirements](#network-requirements))
- Verify the BMC is configured and powered on

### "Authentication failed" when connecting to BMC

- Double-check the BMC username and password
- Some BMC firmware defaults to `admin` / `admin`; others use `ADMIN` / `ADMIN`
- The script supports empty passwords (for BMCs with no password configured) by setting `bmc_password: ""` in the config file
- Run mode 5d (BMC Auth Verify) to test credentials for all nodes without starting a reinit

### Script hangs waiting for LOADER prompt

- Enable `--debug` to see raw console output
- The system may be taking longer than expected to POST
- Some systems require the boot interrupt character multiple times — the script retries automatically
- If the system has already booted past LOADER, perform a manual reset and re-run

### ONTAP cluster wizard not progressing

- Run with `--debug` to watch the wizard in real time
- Check that cluster management IP and gateway values are reachable from the cluster node's management port
- Verify DNS server addresses in the config file are reachable

### `UnboundLocalError` or Python traceback

- Ensure you are using Python 3.6 or later: `python3 --version`
- Confirm the script file was not corrupted during transfer (check file size and line endings)
- If using a config file, validate it is well-formed JSON: `python3 -m json.tool configs/reinit-config.json`

### Log files not created

- The script creates the `logs/` directory relative to `os.getcwd()` at startup
- Ensure the current working directory is writable
- If running via `nohup`, the working directory may differ from the script location; use `cd` to set it explicitly before running

### `--screen` fails with "screen is not installed"

```bash
# Ubuntu/Debian
sudo apt install screen

# RHEL/CentOS/Fedora
sudo dnf install screen
```

Then re-run with `--screen`.

### Can't reattach to the screen session

- List sessions to confirm it is still running: `screen -ls`
- If the session name differs, attach by PID: `screen -r <pid>`
- If the session ended (script finished or crashed), check the summary log under `logs/` for the outcome

---

## Diagnostic Bootargs (`--diag`)

The `--diag` flag enables injection of one-off custom LOADER bootargs during the LOADER stage of a reinit. This is useful for applying special diagnostics or tuning variables (e.g. after `set-defaults` resets them) without modifying the script itself.

### How it works

1. After the config file prompt (and before any BMC connection), the script looks for a `bootargs.txt` or `bootargs` file in `configs/` then the script directory.
2. If found, each non-blank, non-comment line is treated as one bootarg entry.
3. If not found, the operator is prompted to enter bootargs interactively (one per line, blank line to finish).
4. All entries are printed as `setenv option value` and the operator must confirm before the script proceeds. Invalid entries (missing value, `setenv` prefix) cause an immediate exit.
5. Confirmed entries are injected as `setenv <option> <value>` in the LOADER command sequence on **all nodes** (primary and all peers), immediately after `raid.use-physical-zeroing?` is set and before `saveenv`.
6. If the LOADER returns an error response to any `setenv` command, the script prints the error and exits immediately.

### `bootargs.txt` / `bootargs` format

Each non-blank line must be exactly two whitespace-separated tokens: the option name and its value. The name does **not** need to start with `bootarg.` — any `option_name value` pair is accepted. Do **not** include `setenv` — the script adds it. Lines starting with `#` are treated as comments and ignored.

```
# Diagnostic bootargs
bootarg.init.initnonsz 0x80000
bootarg.vm.memmap.efi true
some_option_name 1
```

The file is searched in this order:
1. `configs/bootargs.txt`
2. `configs/bootargs`
3. `./bootargs.txt` (same directory as the script)
4. `./bootargs`

### Entry validation rules

| Rule | What happens on violation |
|---|---|
| Entry must NOT start with `setenv` | Hard exit with message — remove the prefix and re-run |
| Entry must be exactly two tokens: `option_name value` | Hard exit with message — fix the file/input and re-run |
| LOADER responds with `%`, `Error`, `invalid`, or `unknown` after a `setenv` | Script prints the LOADER output and exits |

After loading, all entries are printed as `setenv option value` and the operator must confirm before the script proceeds.

### Usage

```bash
# With a bootargs.txt file present (configs/ or script dir):
python3 AFX_reinit.py --diag

# Without a file — interactive prompt:
python3 AFX_reinit.py --diag
#  ℹ️  No bootargs.txt / bootargs file found. Enter bootargs interactively.
#     Format: option_name <value>   (do NOT include 'setenv')
#     Examples:  bootarg.init.initrd 1   |   some_option true
#     Press Enter on a blank line when done.
#   bootarg> bootarg.init.initnonsz 0x80000
#   bootarg> some_option true
#   bootarg>
#
#   📋 2 diagnostic bootarg(s) to apply:
#      setenv bootarg.init.initnonsz 0x80000
#      setenv some_option true
#
#   Apply these bootargs? [Y/n]:

# Can be combined with any reinit mode:
python3 AFX_reinit.py --diag --resume
python3 AFX_reinit.py --diag --config reinit-config.json
```

### Checkpoint / resume

For mode 4b, the validated bootarg list is saved to the checkpoint file. On `--resume` the stored list is restored automatically — no re-prompt.

---

## Reset to LOADER (`--loader` / mode 5z)

Mode 5z resets selected configured nodes to the `LOADER>` prompt in parallel via BMC. It is a lightweight staging utility — it does not begin a reinit, install software, or modify any configuration. Use it to prepare all nodes or a chosen subset before starting a manual reinit, netboot, or any workflow that requires nodes to be sitting at LOADER.

### How it works

1. Reads the config file for all BMC addresses (primary + all secondary nodes).
2. Shows a numbered target list and lets you choose all nodes or a comma-separated subset by number.
3. Opens a parallel BMC SSH session to each selected node simultaneously.
4. Issues `system reset` to reboot the node.
5. Monitors the console, intercepting the AUTOBOOT countdown with Ctrl+C.
6. Confirms the `LOADER>` prompt on each node and reports success or failure per node.
7. The script exits once all selected nodes have reached LOADER (or timed out).

### Usage

```bash
# Reset all nodes to LOADER prompt in parallel
python3 AFX_reinit.py --loader --config configs/reinit-config.json
```

### Notes

- Requires a config file with BMC addresses for all nodes (`--config`).
- Supports selecting all listed nodes or a numbered subset before reset begins.
- Each node is processed independently; a failure on one node does not stop the others.
- If a node fails to reach LOADER within the timeout, it is reported as failed in the summary — other nodes continue.
- This mode does **not** modify ONTAP or cluster state; it only resets the nodes at the hardware level.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full revision history. The table
below summarizes released versions; the changelog file also tracks the
current `[Unreleased]` working set.

| Version | Date | Description |
|---|---|---|
| v2 (unreleased) | Jun 17, 2026 | **Richer run-summary timing.** The session summary now includes a dedicated **Pause wait** row (aggregate pause-hold time, count, and longest-pause context), per-node **image download** and **image install** subtimings under the netboot install phase, and a named **Auto Join** phase so cluster-join wait time is attributed rather than appearing as unaccounted time. |
| v2 (unreleased) | Jun 13, 2026 | **Runtime pause and checkpoint controls.** Added live pause/resume control for active runs (`.afx_pause`, `SIGUSR1` toggle, `SIGUSR2` resume) that suppresses auto-reconnect while paused, plus manual checkpoint snapshots during runtime (`.afx_checkpoint_now`, `SIGURG`) written as `checkpoints/afx_checkpoint_manual_YYYYMMDD_HHMMSS.json`. |
| v2 (unreleased) | Jun 13, 2026 | **Safer credential prompts.** Config-loaded BMC username prompt now shows `BMC username [admin]:`, and 4b pre-collected cluster admin passwords now require confirmation (`Confirm cluster admin password`) with mismatch retry. |
| v2 (unreleased) | Jun 1, 2026 | **Incremental node join timing.** Per-node sub-rows under `Node join total` now show incremental elapsed time (`+Xm`) for the 2nd and later nodes, making it easy to see how long each individual node join took. First node and `Join → all nodes healthy` retain cumulative totals. |
| v2 (unreleased) | Jun 1, 2026 | **Periodic health-wait heartbeat.** While waiting for all nodes to become healthy, the terminal prints `⏳ Still waiting for N healthy node(s) — elapsed Xm Ys; next check in ~5 min...` every 5 minutes so operators can confirm the script is alive. |
| v2 (unreleased) | Jun 1, 2026 | **DSA host key rejection fix.** Added `disabled_algorithms={"pubkeys": ["ssh-dss"]}` to every `SSHClient.connect()` call site to prevent `q must be exactly 160, 224, or 256 bits long` errors when BMCs or cluster management interfaces present non-standard DSA host keys. |
| v2 (unreleased) | Jun 1, 2026 | **Raw BMC console output suppressed.** BIOS banners, copyright lines, and memory-init text no longer appear in the terminal between "System console connected" and "Now monitoring boot output". Console data still goes to the session log. |
| v2 (unreleased) | Jun 1, 2026 | **Mode 1/3: "same credentials for all peers" prompt.**Before collecting per-peer BMC credentials, the script asks whether to reuse the primary node's username and password for all peers. Answering Y (default) skips all per-node prompts. |
| v2 (unreleased) | Jun 1, 2026 | **Mode 3 crash fix.** `apply_to_globals()` at the peer-list stash step was overwriting `_session_log` with `None` because the `RunContext` snapshot predated `_make_session_log()`. Fixed with `refresh_from_globals()` before the write-back. |
| v2 (unreleased) | Jun 1, 2026 | **5c config gather — complete `reinit-config.json` output.** `primary_node` and `secondary_nodes` blocks are now written correctly. Fixes: ANSI escape codes stripping in PTY output; `(DEPRECATED)-Role` label incorrectly filtered; `IPspace of LIF` label missing from key map; prefix-length (`/16`) netmask support added; all label lookups changed to exact-match. |
| v2 (unreleased) | Jun 1, 2026 | **5c config gather — LIF summary tables.** Retained configuration summary now shows Cluster LIFs and Management LIFs in separate fixed-width tables (with a `role` column in the management table). Dash separators are sized to match actual column widths. |
| v2 (unreleased) | Jun 1, 2026 | **5c config gather — BMC prompt consumed by probe fix.** When connecting via a BMC IP, the initial probe was consuming the BMC `>` prompt before `wait_for_bmc_prompt` ran, causing an immediate timeout. Fixed by checking probe output before deciding whether to wait again. |
| v2 (unreleased) | Jun 1, 2026 | **Default BMC username `admin`.** Options 3 and 5d prompts now show `BMC username [admin]:` and fall back to `admin` on Enter. |
| v2 (unreleased) | Jun 1, 2026 | `--diag` flag: inject custom LOADER bootargs (from `bootargs.txt` / `bootargs` file in `configs/` or script dir, or interactive prompt) after `set-defaults` and before `saveenv` on all nodes. Accepts any `option_name value` format. All entries printed and confirmed before proceeding. Invalid entries (missing value, `setenv` prefix) are a hard exit. Validates format, detects LOADER errors on apply, checkpoints list for resume. |
| v2 (unreleased) | Jun 1, 2026 | Cluster node-healthy wait increased to 15 minutes (was 10), polling every 5 minutes (was 2). |
| v2 (unreleased) | May 29, 2026 | BMC SSH stale session diagnostics: automatic diagnosis + `ipmitool sol deactivate` on every banner-retry; `--auto-clear-stale-bmc` flag SIGTERMs other-Python PIDs holding sockets to the BMC; interactive cleanup offer added to mode 5d when BMC verification fails. |
| v2 (unreleased) | May 28, 2026 | 4a ONTAP upgrade overhaul: BMC picker from existing reinit config / `BMC_IP.json`; cluster login reuses BMC credentials; parallel image install fans out across per-node management IPs (round-robin) with TCP/22 + SSH-auth pre-flight validation; raw cluster command echo suppressed from console (still in log); failover wait polls every 3 min for up to 30 min with live elapsed / remaining status. Interactive prompt-wait telemetry added to session summary (count, total, longest, ≥60 s extended waits, and `Unaccounted time` line). 4b reinit-type-3 now prompts for physical-disk zeroing. |
| v2 | May 15, 2026 | Added `--screen` flag: auto-launches the script inside a detached GNU screen session to protect against SSH disconnections and terminal timeouts. Implies `--bg`. Detects existing screen sessions via `STY` env var to prevent recursion. |
| v2b | Apr 7, 2026 | Parallel peer node operations; end-to-end mode (3); ONTAP upgrade (4a); netboot install (4b); license install (5a); SSH key setup (5b); config backup (5c); BMC auth verify (5d); JSON config file support; background mode; session log with phase/step timing, warnings, and errors inventory. |
| v2a | Apr 7, 2026 | Session logging with timing and summary; warning/error collection in summary; `_recv_loop` + thin wrapper architecture; module-level `_peer_reinit_worker`. |
| v1 | Apr 7, 2026 | Initial release. Modes 1a and 2a. |

---

<details>
<summary><strong>What's New in v2</strong></summary>

| Feature | Description |
|---|---|
| **Boot-menu stall recovery (`Waiting for BMC`)** | During option 2b/3 boot-menu waits, if console output reports `Waiting for BMC` and then stalls, the script now visibly retries BMC SSH + `system console` and continues on the refreshed session. |
| **Boot-menu keepalive (5-minute CR)** | While waiting for long boot transitions, the script now sends a carriage return every 5 minutes to reduce BMC console session timeout risk. |
| **Boot DNA capture via `printenv`** | DNA verification now runs `printenv` and saves raw LOADER environment output to `configs/loader_printenv_<timestamp>.txt` before parsing `bootarg.init.dna`. |
| **LOADER env review safety stop** | After showing the pre/post `set-defaults` env diff during reinit, the script prompts whether to abort the run before any further boot-step changes proceed. |
| **LOADER env utilities (`5i`/`5j`)** | New standalone env tools are available in option 5: `5i` backup LOADER env and `5j` compare env vs defaults. Both are currently marked **(experimental)** in the menu. |
| JSON Config File | Cluster and node credentials can be pre-supplied in a JSON config file, eliminating repeated prompts across multi-node operations. |
| Full Automation Modes | Modes 1b, 2b, and 3 drive the ONTAP cluster setup and node-join wizards without operator interaction. |
| Parallel Node Operations | Mode 2b and Mode 3 run peer node additions in parallel threads, significantly reducing multi-node reinit time. |
| End-to-End Mode (3) | Combines 1b (primary init) + 2b (peer adds) into a single unattended run. |
| **Bulk cluster join (`cluster add-node`)** | Peer nodes now join via ONTAP's native bulk command rather than the per-node interactive wizard. All nodes complete Option 4 / disk erase / node-mgmt in parallel; a single `cluster add-node -cluster-ips` command adds them all at once. Progress is polled every 2 minutes until all nodes show success (up to 15 min). See [End-to-End Reinit Time Estimates](#end-to-end-reinit-time-estimates) for a full comparison — at 64 nodes the new approach saves ~10h vs the old serial join method. |
| **Per-node milestone timing** | The session summary now emits five timestamped milestones per peer node (LOADER, Option 4, disk erase, node-mgmt, cluster IP) plus per-node `cluster add-node` success time. |
| ONTAP Upgrade (4a) | Rolling upgrade via automated takeover/giveback sequence using structured `-fields` polling. Connects directly to the cluster management LIF via SSH (from `reinit-config.json` or a prompted IP) for all ONTAP CLI operations; BMC console is used only as a fallback when direct SSH is unavailable. SSH reconnects automatically if the channel drops mid-upgrade. Post-upgrade version verification and cluster health checks also use the direct SSH channel for reliable, noise-free output. |
| Netboot Install (4b) | Automated ONTAP netboot/software install with optional post-install reinit. |
| Netboot Install Only (4c) | Runs netboot/software install only and stops before reinit or node-add workflows. |
| Install License (5a) | Connects via BMC console and applies a pre-staged license file without running any reinit steps. |
| SSH Key Setup (5b) | Configures passwordless SSH from the script host to cluster management. |
| Config Backup (5c) | Saves or constructs cluster configuration (cluster name, IPs, NTP servers, licenses, nodes) to a JSON file for use in future runs. Accepts a BMC address, cluster management IP, or cluster hostname as the connection target. Captured NTP servers are written to the config; if none are found the operator is offered `pool.ntp.org` as a default. After gather/build paths that connect to an existing cluster, the script also writes `configs/cluster_IP.json` (first cluster-role IP per node, in command-output order) alongside `reinit-config.json`. The retained configuration summary displays Cluster LIFs and Management LIFs in separate tables. |
| BMC Auth Verify (5d) | Batch-tests BMC SSH credentials for loaded BMCs. Shows a numbered target list and supports running against all entries or a comma-separated subset of selected numbers. |
| Reset to LOADER (5z) | Connects to all configured BMC addresses in parallel, issues a system reset on each node, enters the system console, and sends Ctrl+C to interrupt AUTOBOOT. The script exits when every node has reached the LOADER> prompt (or reports failure). Useful for staging all nodes before a manual reinit or netboot run. |
| **Cluster Health Check (5g)** | Connects to the cluster management LIF via SSH and runs `cluster show`, `storage failover show`, `network port show -ipspace Cluster`, and `system image show` to confirm all nodes are healthy and report the running ONTAP version. Cluster-port validation fails the check if any cluster port is not `Link=up` or `Health=healthy`, with detailed per-port warnings. Auto-loads connection details from `reinit-config.json`; if no config is present it offers to run 5c (config gather) first, then returns to the health check automatically. If the cluster shell is not reached or node discovery fails, the check now reports **not healthy** (no false healthy pass). |
| **Stale BMC Session Cleanup (5h)** | Interactive tool to list and clean up stale SSH/SOL connections to BMC/SP addresses. For SSH diagnostics, **one-IP** selection now shows a numbered list of IPs from config (BMC, cluster management, and node management) with labels, and supports entering a custom IP/hostname. Includes explicit actions for `ipmitool sol deactivate` and **Remove BMC from known hosts** (`ssh-keygen -R <BMC IP>`), plus full cleanup (known_hosts reset + drop in-process clients + ipmitool + optional stale-PID SIGTERM). Returns to main menu when done. |
| **Cluster IP manifest builder (5l)** | Adds utility mode **5l** to query cluster-role interfaces (`-role cluster`) from cluster shell and write `configs/cluster_IP.json`. The manifest keeps the first cluster IP per node in command-output order and is used by node-add workflows for deterministic `cluster add-node -cluster-ips` ordering. This mode is currently **EXPERIMENTAL/IN PROGRESS**. |
| **2b upfront cluster-auth decision** | Mode 2b now asks before node-add work begins whether to use current BMC credentials for cluster-network IP lookup, so join automation does not stop later for a mid-run credential prompt. |
| **2b "Add another node" timeout** | The post-join `Add another node to the cluster? [Y/N]` prompt now times out after 5 minutes and defaults to **No**. |
| **Password groups for per-node BMC credentials** | In per-node credential flows, choosing **not** to use the same password now offers `Use password groups? (y/n)`. You can define reusable password groups, assign nodes by numbered list, review a manifest, and restart grouping before proceeding. |
| **2a/2b/3 BMC auth now inherits 4b fallback behavior** | Node-add and end-to-end connect/reconnect paths now silently try fallback credentials (including blank password) before prompting again, reducing manual retries when nodes differ between blank/non-blank passwords. |
| **Blank-password retry handling (1a/1b/2a/2b/3 + utilities)** | Credential retry paths now treat a blank password as an intentional value to try (instead of aborting or silently replacing it with fallback credentials). To skip a retry explicitly, enter `SKIP` where prompted. |
| **Result-screen pause before menu return (5d/5g)** | After BMC auth verify (5d) and cluster health check (5g), the script now waits for **Enter** before returning to the menu so operators can review output without it scrolling away. |
| **2a/2b selective node omission + auto-skip joined nodes** | Modes 2a and 2b now show numbered secondary-node lists and allow comma-separated omission by number before add starts. During add, the script queries `network interface show -role node-mgmt` and automatically omits nodes already present in the cluster. |
| **5b known_hosts opt-in auto-accept** | In manual SSH key setup (5b), the operator can choose to auto-accept known_hosts addition; when enabled, acceptance is performed at the end of the workflow before final SSH verification. |
| Session Logging | Captures per-phase and per-step timing, outcome (PASS/FAIL/WARN), and a complete warning and error inventory in the summary file. |
| **Screen output log** | Every line printed to the terminal during a run is captured to `screen_output_<timestamp>.log` in the session log directory. ANSI codes are stripped for clean plain-text reading. |
| Background Mode | `--bg` flag: handles SIGHUP cleanly so the script can run unattended in a detached or screen session. |
| Screen Mode | `--screen` flag: automatically re-launches the script inside a detached GNU screen session. Protects against SSH disconnections and terminal timeouts. Implies `--bg`. |
| Node add resume | Resumes interrupted node add processes. |
| Physical disk zeroing | Adds option to physically zero disks rather than fast zero (which helps ensure performance consistency). |
| BMC SSH stale session diagnostics | On every banner-retry attempt the script automatically diagnoses stale SSH session slots, closes its own in-process clients, and runs `ipmitool sol deactivate`. `--auto-clear-stale-bmc` adds SIGTERM of other-Python PIDs holding sockets to the BMC. Mode 5d offers interactive remediation when BMC verification fails, including known_hosts reset (`ssh-keygen -R <BMC IP>`). Use option 5h for standalone diagnostics/cleanup with the same known_hosts remediation action. |
| Diagnostic bootarg injection (`--diag`) | Injects custom LOADER `setenv` bootargs (from a `bootargs.txt` or `bootargs` file in `configs/` or the script directory, or interactive prompt) after `set-defaults` and before `saveenv` on all nodes. Accepts any `option_name value` format (not just `bootarg.` prefix). All entries printed and confirmed before proceeding. Validates format, detects LOADER errors on apply, and checkpoints the bootarg list for resume. |

</details>

---

## See Also

- [NetApp ONTAP documentation](https://docs.netapp.com/us-en/ontap/)
- `screen(1)`, `nohup(1)`, `ssh(1)`, `python3(1)`
