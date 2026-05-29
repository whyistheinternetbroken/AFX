#!/usr/bin/env python3

# OPT (phase 2): PEP 563 deferred evaluation of annotations. Lets us add
# type hints freely without paying runtime cost, and without worrying about
# forward references to names defined later in the module. Must precede any
# real imports per Python's __future__-statement rules.
from __future__ import annotations

# ---------------------------------------------------------------------------
# Suppress CryptographyDeprecationWarning BEFORE any other imports
# ---------------------------------------------------------------------------
import warnings
warnings.filterwarnings(
    "ignore",
    message=r".*Python .* is no longer supported.*",
    category=DeprecationWarning,
)

import atexit
import subprocess
import sys
import os
import shutil
import time
import re
import json
import importlib  # OPT: use importlib.import_module instead of __import__
import ipaddress
import getpass
import logging
import threading
import signal
import argparse
import platform
import socket
from datetime import datetime
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field

# OPT: compile LOADER prompt regex once at module level instead of per-iteration
_LOADER_PROMPT_RE = re.compile(r'LOADER-\w+>')

# Cluster shell prompt: e.g. "clustername::>" (admin) or "clustername::*>" (diag)
_CLUSTER_PROMPT_RE = re.compile(r'\S+::\*?>\s*$')

# Loose cluster shell prompt (matches anywhere in tail buffer, not anchored).
# Used by parallel shell command helpers; hoisted to module scope so the
# regex is compiled once instead of on every helper invocation.
_SHELL_PROMPT_RE = re.compile(r"::\*?>")

# ---------------------------------------------------------------------------
# Checkpoint manager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Persists run state to a JSON file so a 4b run can resume from the last
    completed phase rather than starting over from scratch.

    Phases tracked (per-node unless noted):
      ``bmc_connected``       — BMC SSH shell established
      ``at_loader``           — Node reached LOADER prompt
      ``install_done``        — Netboot + option 6 complete; node at ONTAP login
      ``reinit_loader``       — Reconnected to BMC for reinit; back at LOADER
      ``primary_bootmenu_done`` — (global) primary node has cleared the
                                  ONTAP boot menu (option 9 for mode 1b/3,
                                  option 4 for mode 2). Cluster setup
                                  wizard is about to begin.
      ``cluster_formed``      — (global) ``cluster create`` succeeded;
                                cluster shell ``::>`` reachable.
      ``primary_setup_done``  — (global) primary cluster-setup wizard
                                returned successfully (license / SSH
                                post-steps may still be pending).
      ``peer_option4_done``   — (per-peer, mode 3) peer cleared boot
                                menu option 4, finished format, and
                                reached the join barrier. Format work
                                does not need to be re-run on resume.
      ``peer_joined``         — (per-peer, mode 3) peer joined the
                                cluster and ``cluster show`` confirmed
                                membership.
      ``option3_complete``    — (global) mode 3 finalize banner printed;
                                the checkpoint file is then deleted.

    Passwords are intentionally NOT saved; the operator will be asked to
    re-enter them on resume.

    Usage::

        cp = CheckpointManager()
        cp.init_run(mode="4b", bmc_ips=["10.x.x.x"], log_dir="/path")
        cp.mark_node_done("install_done", "10.x.x.x")
        cp.mark_done("cluster_formed")

        # On restart
        cp2 = CheckpointManager()
        if cp2.load():
            if cp2.is_node_done("install_done", "10.x.x.x"):
                ...  # skip install phase

        cp.clear()  # on successful completion
    """

    CHECKPOINT_FILE = "afx_checkpoint.json"
    MAX_AGE_HOURS = 72  # checkpoints older than this are ignored

    def __init__(self, path: "str | None" = None):
        try:
            _script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            _script_dir = os.getcwd()
        self._path = path or os.path.join(_script_dir, self.CHECKPOINT_FILE)
        self._data: "dict" = {}

    # ── Public API ─────────────────────────────────────────────────────────

    def init_run(self, mode: str, bmc_ips: "list[str]",
                 log_dir: str = "", config_path: str = "") -> None:
        """Initialise a fresh checkpoint for a new run. Overwrites any
        existing checkpoint file.
        """
        now = datetime.now().isoformat()
        self._data = {
            "version": 1,
            "created": now,
            "updated": now,
            "mode": mode,
            "log_dir": log_dir,
            "config_path": config_path,
            "bmc_ips": list(bmc_ips),
            "phases": {},        # phase_name -> {"done": bool, "ts": str}
            "node_phases": {},   # phase_name -> {ip -> {"done": bool, "ts": str}}
        }
        self._save()

    def load(self) -> bool:
        """Load a checkpoint from disk.  Returns True if a valid,
        non-expired checkpoint was loaded, False otherwise.
        """
        if not os.path.isfile(self._path):
            return False
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        if data.get("version") != 1:
            return False
        created_str = data.get("created", "")
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str)
                age_hours = (datetime.now() - created_dt).total_seconds() / 3600
                if age_hours > self.MAX_AGE_HOURS:
                    return False
            except Exception:
                pass
        self._data = data
        return True

    def save(self) -> None:
        """Public wrapper — call after modifying state externally."""
        self._save()

    def clear(self) -> None:
        """Delete the checkpoint file (call on successful run completion)."""
        with suppress(Exception):
            os.remove(self._path)
        self._data = {}

    # ── Phase helpers ───────────────────────────────────────────────────────

    def mark_done(self, phase: str) -> None:
        """Mark a global (non-per-node) phase as complete."""
        self._data.setdefault("phases", {})[phase] = {
            "done": True, "ts": datetime.now().isoformat(),
        }
        self._save()

    def is_done(self, phase: str) -> bool:
        """Return True if a global phase is marked complete."""
        return bool(self._data.get("phases", {}).get(phase, {}).get("done"))

    def mark_node_done(self, phase: str, ip: str) -> None:
        """Mark a per-node phase as complete for *ip*."""
        np = self._data.setdefault("node_phases", {})
        np.setdefault(phase, {})[ip] = {
            "done": True, "ts": datetime.now().isoformat(),
        }
        self._save()

    def is_node_done(self, phase: str, ip: str) -> bool:
        """Return True if the per-node phase is marked complete for *ip*."""
        return bool(
            self._data.get("node_phases", {})
                      .get(phase, {})
                      .get(ip, {})
                      .get("done")
        )

    def nodes_done_for(self, phase: str) -> "list[str]":
        """Return the list of IPs that have completed *phase*."""
        return [
            ip for ip, v in
            self._data.get("node_phases", {}).get(phase, {}).items()
            if v.get("done")
        ]

    def summary(self) -> str:
        """Return a human-readable description of the loaded checkpoint.

        Lists run metadata (mode, created/updated timestamps, log dir, config
        path), every global phase recorded, and per-node phase completion
        keyed by BMC IP. Intended for the ``--checkpoint-status`` CLI flag so
        an operator can see exactly where a previous run left off without
        opening the JSON file by hand.
        """
        if not self._data:
            return f"No checkpoint loaded (file: {self._path})."

        lines = []
        lines.append(f"Checkpoint file : {self._path}")
        lines.append(f"Mode            : {self._data.get('mode', '?')}")
        lines.append(f"Created         : {self._data.get('created', '?')}")
        lines.append(f"Updated         : {self._data.get('updated', '?')}")
        try:
            created_dt = datetime.fromisoformat(self._data.get("created", ""))
            age_min = int((datetime.now() - created_dt).total_seconds() // 60)
            lines.append(f"Age             : {age_min} minute(s)")
        except Exception:
            pass
        if self._data.get("log_dir"):
            lines.append(f"Log directory   : {self._data['log_dir']}")
        if self._data.get("config_path"):
            lines.append(f"Config path     : {self._data['config_path']}")
        bmc_ips = self._data.get("bmc_ips") or []
        if bmc_ips:
            lines.append(f"BMC IPs         : {', '.join(bmc_ips)}")

        phases = self._data.get("phases") or {}
        lines.append("")
        lines.append("Global phases:")
        if not phases:
            lines.append("  (none recorded)")
        else:
            for name, meta in phases.items():
                mark = "✅" if meta.get("done") else "  "
                ts = meta.get("ts", "")
                lines.append(f"  {mark} {name}  {ts}")

        node_phases = self._data.get("node_phases") or {}
        lines.append("")
        lines.append("Per-node phases:")
        if not node_phases:
            lines.append("  (none recorded)")
        else:
            # Build a phase x ip matrix so each node's progress is easy to scan.
            all_ips = sorted({
                ip for per_ip in node_phases.values() for ip in per_ip.keys()
            } | set(bmc_ips))
            phase_names = list(node_phases.keys())
            for ip in all_ips:
                completed = [
                    p for p in phase_names
                    if node_phases.get(p, {}).get(ip, {}).get("done")
                ]
                pending = [p for p in phase_names if p not in completed]
                lines.append(f"  [{ip}]")
                lines.append(f"      done    : {', '.join(completed) or '(none)'}")
                if pending:
                    lines.append(f"      pending : {', '.join(pending)}")

        return "\n".join(lines)

    # ── Accessors ───────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._data.get("mode", "")

    @property
    def log_dir(self) -> str:
        return self._data.get("log_dir", "")

    @property
    def config_path(self) -> str:
        return self._data.get("config_path", "")

    @property
    def bmc_ips(self) -> "list[str]":
        return list(self._data.get("bmc_ips", []))

    @property
    def created(self) -> str:
        return self._data.get("created", "")

    # ── Internal ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        self._data["updated"] = datetime.now().isoformat()
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass


# Module-level checkpoint instance (initialised by _run_4b_standalone on each
# run; may be loaded from disk on resume).
_checkpoint: "CheckpointManager | None" = None


# Boot-menu detection signatures shared by all wait-for-boot-menu callers.
_BOOT_MENU_SIGS = [
    "selection (1-9)?",
    "boot menu",
    "please choose one of the following boot options",
    "option",
]

# Unique substring that appears in the BMC shell prompt (e.g. "node-bmc>").
# Used to detect accidental console drops mid-wait.
_BMC_PROMPT_SIG = "bmc>"

# Retained-from-existing-cluster state (mode 1, optional reuse after reinit)
_retained_cluster_name = None
_retained_net_config = None  # list[dict] of LIF rows
_retained_default_gateway = None  # str (IPv4) — first 0.0.0.0/0 route gateway
_retained_cluster_contact = None  # str — from `cluster identity show`
_retained_cluster_location = None  # str — from `cluster identity show`
_retained_dns_domains = None       # str — comma-separated, from `dns show`
_retained_dns_servers = None       # str — comma-separated, from `dns show`

# Mapping captured during retain phase to correlate per-node data:
#   _retained_sp_to_node : {sp_address(str) -> ontap_node_name(str)}
# Built from `service-processor show -fields address,node` output.
_retained_sp_to_node = {}

# Pre-answered retain choices. When the operator skips the JSON config file
# at startup, we ask "do you want to reuse the existing cluster
# configuration?" up-front and stash the answers here. main() then skips
# the duplicate retain prompts later. None means "not yet answered;
# prompt as before".
_retain_preselected = None  # None or tuple(retain_name: bool, retain_network: bool, retain_creds: bool)

# Per-BMC node management network config gathered up-front. Keyed by BMC
# host/IP. Each value is a dict with keys: port, ip, netmask, gateway. Used by
# mode 1b auto-init to answer node-management setup prompts for the node
# whose BMC we're driving.
_node_mgmt_by_bmc = {}

# Per-peer-BMC SSH credentials gathered up-front. Keyed by BMC host/IP. Each
# value is a dict with keys: user, password. Defaults to the primary BMC's
# credentials but the operator can override per peer.
_peer_bmc_creds = {}

# Mode 3: list of peer BMCs to auto-add in parallel AFTER primary cluster is
# created. Populated in main() and consumed at the tail of the cluster setup
# wizard.
_peer_bmc_list = []

# Peer node log paths: {ip: log_file_path} for nodes being added during
# cluster setup.  Populated by _run_4b_standalone; read in
# _run_cluster_setup_wizard to display log paths at the node-add transition.
_peer_log_paths: dict = {}

# Mode 2b: tracks BMC hosts that have already been added in the current run so
# they are not offered again when prompting for the next node to add.
_2b_processed_bmcs: set = set()

# Total node count of the cluster BEFORE this reinit run (1 = primary only).
# Set once in main() after the operator confirms the node list. Used to gate
# `cluster ha modify` — that command only applies to 2-node clusters.
_initial_node_count = 0

# True when the operator chooses static LOADER ifconfig instead of -auto.
# Set upfront by _run_4b_standalone; read by _install_worker.
_netboot_static_ip = False

# Path to the most recently written node-add manifest file (set by
# _write_node_add_manifest). Used by option 2c to locate the last run.
_last_node_add_manifest: str = ""

# Set to True when the operator requests passwordless SSH setup during 1a/1b.
_setup_passwordless_ssh = False

# Serializes the join step across parallel peer-add threads. Only one peer
# may be at the "create or join" prompt / cluster-show verification at a
# time so a freshly-joined node is fully recognised before the next one
# kicks off its join.
_join_lock = threading.Lock()

# Serializes access to the primary's logged-in cluster shell channel from
# multiple peer-add threads (used to run `cluster show` verification).
_primary_shell_lock = threading.Lock()

# Serializes interactive credential prompts so background peer-add threads
# don't compete for stdin when multiple BMCs auth-fail concurrently.
_stdin_lock = threading.Lock()

# ── Stale BMC SSH session tracking ───────────────────────────────────────
# Registry of every paramiko SSHClient we have opened to a BMC, keyed by
# host. Populated by _register_bmc_client() on successful connect; drained
# by _drop_bmc_clients_for() when we hit a banner timeout and suspect the
# BMC's tiny session pool may be exhausted by our own leaked clients.
_bmc_clients_by_host: "dict[str, list]" = {}
_bmc_clients_lock = threading.Lock()

# Set by --auto-clear-stale-bmc at startup. When True, the banner-retry
# helper will (a) scan for local TCP sockets to <bmc>:22 owned by other
# python processes and SIGTERM them, and (b) call 'ipmitool sol deactivate'
# if ipmitool is on PATH and IPMI creds are available.
_auto_clear_stale_bmc = False

# Loaded configuration data (from --config or the interactive prompt). Empty
# dict when not provided. Schema described in _CONFIG_FILE_EXAMPLE below.
_config_data = {}

# Cluster-level setup config gathered up-front (used by mode 1b to drive the
# post-node-mgmt cluster setup wizard non-interactively).
_cluster_config = {}

# License settings populated by _collect_license_config().
# Initialised here so _run_cluster_setup_wizard / _apply_license can safely
# reference them even when _collect_license_config() was never called.
_license_mode      = None   # "key", "file", or None (skip)
_license_keys      = []     # list of key strings (mode "key")
_license_file_path = None   # path to .nlf bundle (mode "file")

# Mode 2b: when set after a successful node join, signals main() to drive a
# fresh BMC through the same join pipeline. Tuple of (bmc_host, user, pass).
_add_another_node_request = None

_CONFIG_FILE_EXAMPLE = '''\
Example configuration file (JSON). Any field may be omitted OR left as an
empty string ("").

  * Field omitted (key not present in the JSON) -> the script prompts you
    for the value at runtime. Use this for secrets you don't want stored
    on disk.
  * Field present with empty string ("")        -> the value is used as-is
    with NO prompt. For passwords this means "no password" (e.g. BMCs
    that don't require one or that accept passthrough credentials). For
    other fields it means the empty string will be sent verbatim.
  * Field present with a non-empty value        -> used directly.

"primary_node" is the node used to initialize the cluster (options 1a/1b/3).
"secondary_nodes" is the list of nodes that are added to the cluster
(options 2a/2b and the node-add phase of option 3).
The primary node is NEVER included in secondary_nodes and vice-versa.

Legacy format: a flat "nodes" array (position 0 = primary) is still
accepted for backward compatibility.

{
  "cluster": {
    "name":              "rtp-afx1k-c01",
    "clus_mgmt_address": "10.0.0.100",
    "clus_mgmt_mask":    "255.255.255.0",
    "clus_mgmt_gw":      "10.0.0.1",
    "clus_mgmt_port":    "e0M",
    "user":              "admin",
    "password":          "password",
    "dns_domains":       "ntap.local",
    "dns_servers":       "10.193.67.228,10.193.67.236",
    "location":          "Lab Rack 1",
    "contact":           "ops@example.com"
  },
  "primary_node": {
    "bmc":                "10.192.160.29",
    "bmc_user":           "admin",
    "bmc_password":       "NetApp1!AFX",
    "node_mgmt_port":     "e0M",
    "node_mgmt_ip":       "10.192.160.28",
    "node_mgmt_netmask":  "255.255.255.0",
    "node_mgmt_gateway":  "10.192.160.1"
  },
  "secondary_nodes": [
    {
      "bmc":                "10.192.160.35",
      "bmc_user":           "admin",
      "bmc_password":       "NetApp1!AFX",
      "node_mgmt_port":     "e0M",
      "node_mgmt_ip":       "10.192.160.30",
      "node_mgmt_netmask":  "255.255.255.0",
      "node_mgmt_gateway":  "10.192.160.1"
    }
  ]
}
'''


# ── RunContext ──────────────────────────────────────────────────────────────
# A single bag of configuration/state that is currently scattered across
# ~30 module-level globals. Introducing this dataclass is the first
# foundation step toward extracting the shared 1/2/3 inline flow out of
# main() without growing function signatures to 15+ parameters.
#
# Phase 1 (this commit): define the dataclass and provide round-trip
# helpers (``from_globals()`` / ``apply_to_globals()``). No call sites are
# migrated yet — globals remain the source of truth. The class lets future
# extractions accept a single ``ctx: RunContext`` parameter instead of
# touching the globals directly.
#
# Phase 2 (later commits): instantiate ``_run_context`` once in ``main()``
# after CLI/config parsing and migrate helpers one at a time to read/write
# through it. Concurrency primitives (locks, events) intentionally stay as
# module globals — they are process-wide singletons, not run state.

# Mapping of RunContext field -> module global name. Kept here so the
# round-trip helpers stay in sync as fields are added/removed.
_RUN_CONTEXT_FIELD_TO_GLOBAL = {
    # Mode / CLI flags
    "operation_mode":           "_operation_mode",
    "auto_setup":               "_auto_setup",
    "auto_add":                 "_auto_add",
    "bg_mode":                  "_bg_mode",
    "debug_console":            "_debug_console",
    # Cluster / general config
    "cluster_config":           "_cluster_config",
    "config_data":              "_config_data",
    # Retained-from-existing-cluster state
    "retained_cluster_name":     "_retained_cluster_name",
    "retained_net_config":       "_retained_net_config",
    "retained_default_gateway":  "_retained_default_gateway",
    "retained_cluster_contact":  "_retained_cluster_contact",
    "retained_cluster_location": "_retained_cluster_location",
    "retained_dns_domains":      "_retained_dns_domains",
    "retained_dns_servers":      "_retained_dns_servers",
    "retained_sp_to_node":       "_retained_sp_to_node",
    "retain_preselected":        "_retain_preselected",
    # Peer / BMC state
    "peer_bmc_list":            "_peer_bmc_list",
    "peer_bmc_creds":           "_peer_bmc_creds",
    "node_mgmt_by_bmc":         "_node_mgmt_by_bmc",
    "primary_bmc_user":         "_primary_bmc_user",
    "primary_bmc_password":     "_primary_bmc_password",
    "initial_node_count":       "_initial_node_count",
    "add_another_node_request": "_add_another_node_request",
    # Licensing
    "license_mode":             "_license_mode",
    "license_keys":             "_license_keys",
    "license_file_path":        "_license_file_path",
    # Behavior flags
    "netboot_static_ip":        "_netboot_static_ip",
    "setup_passwordless_ssh":   "_setup_passwordless_ssh",
    "netboot_before_reinit":    "_netboot_before_reinit",
    "physical_zeroing":         "_physical_zeroing",
    # Run-scoped state objects
    "session_log":              "_session_log",
    "checkpoint":               "_checkpoint",
}


@dataclass
class RunContext:
    """Run-scoped configuration and state for a single invocation.

    Mirrors a curated subset of module globals. Use ``from_globals()`` to
    snapshot the current globals into a fresh context and
    ``apply_to_globals()`` to push field values back. Concurrency primitives
    (``_join_lock``, ``_shutdown_event``, etc.) are deliberately excluded.
    """
    # Mode / CLI flags
    operation_mode: "int | None" = None
    auto_setup: bool = False
    auto_add: bool = False
    bg_mode: bool = False
    debug_console: bool = False

    # Cluster / general config
    cluster_config: dict = field(default_factory=dict)
    config_data: dict = field(default_factory=dict)

    # Retained-from-existing-cluster state
    retained_cluster_name: "str | None" = None
    retained_net_config: "list | None" = None
    retained_default_gateway: "str | None" = None
    retained_cluster_contact: "str | None" = None
    retained_cluster_location: "str | None" = None
    retained_dns_domains: "str | None" = None
    retained_dns_servers: "str | None" = None
    retained_sp_to_node: dict = field(default_factory=dict)
    retain_preselected: "tuple | None" = None

    # Peer / BMC state
    peer_bmc_list: list = field(default_factory=list)
    peer_bmc_creds: dict = field(default_factory=dict)
    node_mgmt_by_bmc: dict = field(default_factory=dict)
    primary_bmc_user: "str | None" = None
    primary_bmc_password: "str | None" = None
    initial_node_count: int = 0
    add_another_node_request: "tuple | None" = None

    # Licensing
    license_mode: "str | None" = None
    license_keys: list = field(default_factory=list)
    license_file_path: "str | None" = None

    # Behavior flags
    netboot_static_ip: bool = False
    setup_passwordless_ssh: bool = False
    netboot_before_reinit: bool = False
    physical_zeroing: bool = False

    # Run-scoped state objects (kept as Any to avoid forward refs at runtime)
    session_log: "object | None" = None
    checkpoint: "object | None" = None

    @classmethod
    def from_globals(cls) -> "RunContext":
        """Snapshot the current module globals into a fresh RunContext.

        For container fields (dict/list) the global value is referenced
        directly, not copied — so in-place mutations on the global remain
        visible through the context (and vice-versa) until call sites are
        fully migrated. Scalars are simple value copies.
        """
        g = globals()
        kwargs = {}
        for field_name, global_name in _RUN_CONTEXT_FIELD_TO_GLOBAL.items():
            if global_name in g:
                kwargs[field_name] = g[global_name]
        return cls(**kwargs)

    def apply_to_globals(self) -> None:
        """Push every field back into its corresponding module global.

        Used after a helper mutates the context (e.g. fills in cluster_config
        from a wizard) and the legacy global-based callers further down the
        flow still need to see the new value.
        """
        g = globals()
        for field_name, global_name in _RUN_CONTEXT_FIELD_TO_GLOBAL.items():
            g[global_name] = getattr(self, field_name)

    def refresh_from_globals(self) -> None:
        """Pull current global values into this context in-place. Useful
        when a legacy code path has updated the globals and we want the
        context view to catch up before passing it to a migrated helper.
        """
        g = globals()
        for field_name, global_name in _RUN_CONTEXT_FIELD_TO_GLOBAL.items():
            if global_name in g:
                setattr(self, field_name, g[global_name])


# Module-level RunContext instance, instantiated by main() once CLI/config
# parsing is complete. Stays ``None`` until then so any code that runs
# during module import doesn't accidentally see a half-built context.
_run_context: "RunContext | None" = None


def _ctx_sync_from_globals() -> None:
    """If the module-level RunContext exists, refresh its fields from globals.

    Call this after a legacy code path rebinds a global mapped into the
    RunContext (e.g. ``_config_data = {}``), so the context view doesn't
    drift away from the live global.
    """
    if _run_context is not None:
        _run_context.refresh_from_globals()


def _prompt(prompt: str, default: str = "") -> str:
    """``input(prompt).strip()`` with EOFError/KeyboardInterrupt swallowed.

    Returns ``default`` if the user interrupts (Ctrl+C/Ctrl+D). Whitespace
    is always trimmed from the real input. Callers that need lowercase can
    chain ``.lower()`` on the return value.
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default


# ── Interactive-prompt wait-time tracker ────────────────────────────────────
# Wraps the builtin ``input`` once at import time so every blocking prompt
# (whether it goes through ``_prompt`` or calls ``input`` directly) is
# measured. The accumulated wait time is surfaced in the run summary so that
# operator idle time at prompts can be distinguished from real work time and
# the unaccounted-time gap between phase totals and wall-clock runtime is
# explained.
import builtins as _builtins

_prompt_wait_seconds = 0.0
_prompt_wait_count = 0
_prompt_wait_lock = threading.Lock()
_prompt_wait_max = 0.0
_prompt_wait_max_label = ""
# Threshold (seconds) above which a single prompt is considered an
# "extended" operator wait worth flagging in the summary.
_PROMPT_WAIT_EXTENDED_THRESHOLD = 60.0
_prompt_wait_extended = []  # list[(label, seconds)]

_orig_input = _builtins.input


def _tracked_input(prompt=""):
    """Drop-in replacement for the builtin ``input`` that records how long
    the operator spent at each prompt. Behaves identically otherwise; raised
    exceptions (EOFError/KeyboardInterrupt) are re-raised after the wait
    time is still recorded so the totals include aborted prompts.
    """
    global _prompt_wait_seconds, _prompt_wait_count
    global _prompt_wait_max, _prompt_wait_max_label
    _start = time.monotonic()
    try:
        return _orig_input(prompt)
    finally:
        _elapsed = time.monotonic() - _start
        # Strip ANSI/whitespace from the label so the summary stays readable.
        try:
            _label = re.sub(r"\x1b\[[0-9;]*m", "", str(prompt))
            _label = _label.strip().splitlines()[-1] if _label.strip() else ""
            if len(_label) > 60:
                _label = _label[:57] + "..."
        except Exception:
            _label = ""
        with _prompt_wait_lock:
            _prompt_wait_seconds += _elapsed
            _prompt_wait_count += 1
            if _elapsed > _prompt_wait_max:
                _prompt_wait_max = _elapsed
                _prompt_wait_max_label = _label
            if _elapsed >= _PROMPT_WAIT_EXTENDED_THRESHOLD:
                _prompt_wait_extended.append((_label, _elapsed))


_builtins.input = _tracked_input


# ── Console echo suppression ────────────────────────────────────────────────
# When True, _recv_loop / drain_channel skip writing raw BMC/cluster console
# chunks to stdout. They still forward chunks to the session log file via
# _session_log.log_console, so nothing is lost — just hidden from the
# operator's terminal during noisy stages (e.g. system console hand-off and
# cluster login probing).
_console_quiet = False


@contextmanager
def _suppress_console():
    """Temporarily silence raw console echo to stdout while leaving session
    log capture intact. Restored on exit even if the block raises.
    """
    global _console_quiet
    _prev = _console_quiet
    _console_quiet = True
    try:
        yield
    finally:
        _console_quiet = _prev


def _print_banner(title: str, *, width: int = 60) -> None:
    """Print a ``"=" * width`` divider, a ``"  <title>"`` line, then another
    divider. Equivalent to the common 3-line banner block sprinkled across
    the script.
    """
    bar = "=" * width
    print("\n" + bar)
    print(f"  {title}")
    print(bar)


def _open_shell(client, **kwargs):
    """``client.invoke_shell(**kwargs)`` with ``settimeout(0)`` already
    applied. Returns the channel.
    """
    ch = client.invoke_shell(**kwargs)
    ch.settimeout(0)
    return ch


def _reach_bmc_prompt(ch, *, timeout=15, node_log=None, takeover_msg=None):
    """Read until the BMC ``'>'`` prompt, transparently answering ``y`` to a
    ``y/n`` takeover prompt if another session is active.

    Returns ``True`` if the ``'>'`` prompt was reached (immediately or after
    takeover), ``False`` if no prompt was observed within *timeout*. When a
    takeover happens and *takeover_msg* is non-empty, it is forwarded to
    ``_slog`` so the operator sees a single consistent log line.
    """
    _out, matched = direct_read_until_any(
        ch, ["y/n", ">"], timeout=timeout, node_log=node_log,
    )
    if matched and "y/n" in matched.lower():
        if takeover_msg:
            _slog(takeover_msg)
        ch.send("y\r")
        time.sleep(2)
        direct_read_until(ch, ">", timeout=timeout, node_log=node_log)
        return True
    return bool(matched and ">" in matched)


def load_config_file(path: str) -> dict:
    """Load and validate a JSON configuration file. Returns dict on success,
    raises ValueError with a friendly message on failure.
    """
    if not os.path.isfile(path):
        raise ValueError(f"Config file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"Config file root must be a JSON object: {path}")
    return data


# ── Config file discovery helpers ───────────────────────────────────────────
# Multiple code paths (modes 1/3 startup, mode 44 license-only, mode 46
# gather/add, mode 47 BMC list, the 4b reinit re-prompt) all need to locate a
# reinit-style JSON config. Previously the same search loop was copy-pasted in
# 4+ places with subtle variants (different filename lists, different search
# dirs, some with interactive selection, some with silent first-match). These
# helpers centralise the search so callers only choose: (a) the candidate
# filename list, (b) the search directories, and (c) whether to deep-scan
# arbitrary JSONs as a fallback.

# Canonical reinit-config filename list, ordered by preference.
_DEFAULT_CONFIG_FILENAMES = (
    "reinit-config.json",
    "reinit_config.json",
    "reinit-afx-config.json",
    "reinit_afx_config.json",
    "afx-reinit-config.json",
    "config.json",
)


def _script_dir() -> str:
    """Return the directory holding this script. Falls back to CWD when
    ``__file__`` is undefined (e.g. interactive ``exec()`` contexts).
    """
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


def _default_config_search_dirs() -> list:
    """Return the canonical search order: ``configs/`` subdir, the script
    directory, then CWD (skipped if it duplicates one of the first two).
    """
    sd = _script_dir()
    cwd = os.getcwd()
    dirs = [os.path.join(sd, "configs"), sd]
    if os.path.abspath(cwd) not in [os.path.abspath(d) for d in dirs]:
        dirs.append(cwd)
    return dirs


def _find_config_files(candidate_names=None, search_dirs=None,
                       deep_scan=False, deep_scan_keys=("cluster", "nodes")):
    """Return a list of absolute config-file paths discovered on disk.

    ``candidate_names``: filenames to look for in priority order; defaults to
    ``_DEFAULT_CONFIG_FILENAMES``.
    ``search_dirs``: directories to scan in order; defaults to
    ``_default_config_search_dirs()``.
    ``deep_scan``: when True and no exact-name match is found, also scan
    every ``*.json`` in the search dirs and keep those whose top-level dict
    contains ALL of ``deep_scan_keys``. This catches oddly-named files.

    Order of returned paths follows search-dir order, then filename order.
    Duplicates (by absolute path) are filtered.
    """
    names = candidate_names or _DEFAULT_CONFIG_FILENAMES
    dirs = search_dirs or _default_config_search_dirs()

    found = []
    seen = set()
    for d in dirs:
        for name in names:
            p = os.path.join(d, name)
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            if os.path.isfile(p):
                found.append(p)
                seen.add(ap)

    if found or not deep_scan:
        return found

    # Fallback: scan *.json in the search dirs and keep schema-matching ones.
    for d in dirs:
        try:
            for fn in sorted(os.listdir(d)):
                if not fn.lower().endswith(".json"):
                    continue
                p = os.path.join(d, fn)
                ap = os.path.abspath(p)
                if ap in seen or not os.path.isfile(p):
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if all(k in data for k in deep_scan_keys):
                    found.append(p)
                    seen.add(ap)
        except OSError:
            continue
    return found


def _select_config_path_interactive(detected, indent="  ", header_emoji="📄"):
    """Given a list of detected config paths, prompt the operator to pick
    one. Returns the chosen path, or ``None`` if the operator declines.

    For one detected file the prompt is yes/no (default yes). For multiple,
    a numbered selector is shown with ``0`` meaning "none".
    """
    if not detected:
        return None
    pad = indent
    if len(detected) == 1:
        found = detected[0]
        print(f"\n{pad}{header_emoji} Found config file: {found}")
        use_it = _prompt(f"{pad}Use this config file? [Y/N]: ", "n").lower()
        return found if use_it in ("", "y", "yes") else None

    print(f"\n{pad}{header_emoji} Found multiple config files:")
    for i, p in enumerate(detected, 1):
        print(f"{pad}   {i}. {p}")
    print(f"{pad}   0. None — continue without a config file")
    while True:
        sel = _prompt(
            f"{pad}Select [0-{len(detected)}, default 1]: "
        , "0")
        if sel == "":
            sel = "1"
        if not sel.isdigit():
            print(f"{pad}  ⚠️  Enter a number.")
            continue
        idx = int(sel)
        if idx == 0:
            return None
        if 1 <= idx <= len(detected):
            return detected[idx - 1]
        print(f"{pad}  ⚠️  Out of range.")


def _run_parallel(items, target, *, with_index=False, join_timeout=None):
    """Spawn one daemon thread per item, then join them all.

    - ``target`` is called as ``target(item)`` by default, or
      ``target(item, idx)`` when ``with_index=True``.
    - ``join_timeout`` (seconds) is forwarded to each ``Thread.join`` call
      (``None`` waits indefinitely).
    - Returns the list of threads (already joined).
    """
    threads = []
    for idx, item in enumerate(items):
        args = (item, idx) if with_index else (item,)
        t = threading.Thread(target=target, args=args, daemon=True)
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=join_timeout) if join_timeout is not None else t.join()
    return threads


def _bmc_fallback_passwords(ip, bmc_passwords):
    """Build the silent-fallback password list for a BMC connect attempt.

    Currently appends the cluster admin password (if it was collected and
    differs from the BMC's own password) so the connect helper can retry
    silently when the BMC happens to be using cluster admin creds.
    """
    cluster_admin_pw = (_cluster_config.get("admin_password")
                        if isinstance(_cluster_config, dict) else None)
    fb = []
    if cluster_admin_pw and cluster_admin_pw != bmc_passwords.get(ip):
        fb.append(cluster_admin_pw)
    return fb


def _discover_and_prompt_config(ctx):
    """Discover config files and prompt the operator to select one.

    Writes ``ctx.config_data`` and ``ctx.retain_preselected`` and syncs back
    to the legacy module globals via ``ctx.apply_to_globals()``. Used by the
    4b reinit path so that the same config file that drives a normal
    1a/1b/3 run is offered when reinit is triggered from within 4b.

    Returns the config path that was loaded, or None.
    """
    # Pull current globals into ctx so the final apply_to_globals() does not
    # clobber fields that legacy code (e.g. 4b's reinit-mode selection that
    # set _operation_mode / _auto_setup / _auto_add) updated since the ctx
    # was last snapshotted.
    ctx.refresh_from_globals()

    detected_configs = _find_config_files(deep_scan=True)
    config_path = _select_config_path_interactive(detected_configs)

    if not detected_configs and not config_path:
        print("\n  ℹ️  No config file auto-detected.")
        ans = _prompt("  Enter path to a JSON config file, or blank to skip: ")
        if ans:
            if len(ans) >= 2 and ans[0] == ans[-1] and ans[0] in ("'", '"'):
                ans = ans[1:-1]
            expanded = os.path.expanduser(os.path.expandvars(ans))
            if os.path.isfile(expanded):
                config_path = expanded
            else:
                print(f"  ⚠️  File not found: {expanded}; continuing without config.")

    if config_path:
        try:
            ctx.config_data = load_config_file(config_path)
            print(f"  📄 Loaded config: {config_path}")
            ctx.retain_preselected = (False, False, False)
        except ValueError as e:
            print(f"  ⚠️  {e}")
            print("  Continuing without a config file (manual prompts).")
            ctx.config_data = {}
            config_path = None

    ctx.apply_to_globals()
    return config_path


def _cd(ctx=None) -> dict:
    """Resolve the active config-data dict.

    Preference order: explicit ``ctx`` argument → module-level
    ``_run_context`` → legacy ``_config_data`` global. Returns ``{}`` if all
    three are absent. Keeps callers decoupled from the global as helpers
    migrate toward ctx-style parameters.
    """
    if ctx is not None:
        return ctx.config_data or {}
    if _run_context is not None:
        return _run_context.config_data or {}
    return _config_data or {}


def _node_cfg_for(bmc: str, ctx=None) -> dict:
    """Return the node entry from the config that matches `bmc`, or {}.
    Searches primary_node, secondary_nodes, and legacy nodes[].
    """
    cd = _cd(ctx)
    pn = cd.get("primary_node")
    if isinstance(pn, dict) and pn.get("bmc") == bmc:
        return pn
    for node in (cd.get("secondary_nodes") or []):
        if isinstance(node, dict) and node.get("bmc") == bmc:
            return node
    for node in (cd.get("nodes") or []):
        if isinstance(node, dict) and node.get("bmc") == bmc:
            return node
    return {}


def _config_primary_node(ctx=None) -> dict:
    """Return the primary node dict from config (new or legacy format), or {}."""
    cd = _cd(ctx)
    pn = cd.get("primary_node")
    if isinstance(pn, dict) and pn.get("bmc"):
        return pn
    nodes = cd.get("nodes") or []
    if nodes and isinstance(nodes[0], dict):
        return nodes[0]
    return {}


def _config_secondary_nodes(ctx=None) -> list:
    """Return the list of secondary (peer) node dicts from config, or []."""
    cd = _cd(ctx)
    sn = cd.get("secondary_nodes")
    if isinstance(sn, list):
        return [n for n in sn if isinstance(n, dict)]
    nodes = cd.get("nodes") or []
    return [n for n in nodes[1:] if isinstance(n, dict)]

# ---------------------------------------------------------------------------
# Per-node log writer – redirects sys.stdout during automated phases
# ---------------------------------------------------------------------------

# Saved reference to the real terminal stdout; set once at startup so it
# survives any later sys.stdout replacement.
_real_stdout = sys.stdout

# When True (--debug flag), _NodeLogWriter forwards ALL output to the terminal
# instead of filtering to milestone lines only.
_debug_console = False

# ---------------------------------------------------------------------------
# Terminal state restoration
# ---------------------------------------------------------------------------
# getpass and paramiko temporarily set stdin to raw / no-echo mode.  If the
# script is interrupted mid-call the terminal is left broken (no echo, history
# keys not working).  We snapshot the TTY attributes at startup and restore
# them on every exit path, including the os._exit() force-exit path that
# normally bypasses atexit.

_saved_term_attrs = None
_term_fd = None
try:
    import termios as _termios_mod
    _term_fd = sys.stdin.fileno()
    if os.isatty(_term_fd):
        _saved_term_attrs = _termios_mod.tcgetattr(_term_fd)
except Exception:
    pass


def _restore_terminal():
    """Restore TTY to the state captured at script startup."""
    try:
        if _saved_term_attrs is not None and _term_fd is not None:
            import termios as _t
            _t.tcsetattr(_term_fd, _t.TCSADRAIN, _saved_term_attrs)
    except Exception:
        pass
    # Belt-and-suspenders: stty sane resets the line discipline even when
    # the saved snapshot pre-dates a getpass/paramiko raw-mode transition.
    try:
        if sys.platform != "win32":
            subprocess.run(
                ["stty", "sane"],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                check=False,
            )
    except (FileNotFoundError, OSError):
        pass


atexit.register(_restore_terminal)


class _NodeLogWriter:
    """A sys.stdout replacement that tees all output to a per-node log file.

    In *filtering* mode (``interactive=False``, the default for automated
    phases) only milestone lines (those that begin with one of the emoji /
    keyword prefixes defined below) are forwarded to the real terminal.  All
    other output – raw console chunks, verbose status lines – goes to the log
    file only.

    In *pass-through* mode (``interactive=True``, set around
    InteractiveSession.run() and for the interactive-only modes 1a/2a) every
    byte is written to both the log file *and* the real terminal, preserving
    the existing interactive experience.
    """

    _MILESTONE_STARTS = (
        "✅", "❌", "⚠️", "🤖", "🔄", "📋", "🔢", "🔒",
        "📡", "📝", "🌐", "🧩", "🔁", "🛑", "⏳", "↻",
        "Now monitoring boot output",
        "Mode 1b", "Mode 2b", "Auto-init", "Auto-join",
        "Monitoring for AUTOBOOT", "Detected LOADER prompt",
        "Boot menu", "Selecting option",
    )

    def __init__(self, node_file, interactive: bool = False):
        self._nf = node_file
        self.interactive = interactive
        self._buf = ""          # incomplete-line buffer for milestone scan

    def write(self, data: str) -> None:
        # Always write to the per-node log file.
        try:
            self._nf.write(data)
        except Exception:
            pass

        if self.interactive or _debug_console:
            # Pass-through: also write to real terminal.
            _real_stdout.write(data)
            _real_stdout.flush()
            return

        # Filtering mode: scan completed lines for milestone prefixes.
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            stripped = line.lstrip()
            if stripped and any(stripped.startswith(m) for m in self._MILESTONE_STARTS):
                _real_stdout.write(line + "\n")
                _real_stdout.flush()

    def flush(self) -> None:
        try:
            self._nf.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("fileno not supported on _NodeLogWriter")


# ---------------------------------------------------------------------------
# Session logging with phase timing
# ---------------------------------------------------------------------------

class SessionLogger:
    # Background heartbeat tick interval (seconds). While a step is running
    # the logger writes a "still running" line at this cadence so long
    # operations are visible in the log file as time progresses, without
    # cluttering the console.
    _HEARTBEAT_INTERVAL = 15.0

    def __init__(self, bg_mode: bool = False):
        try:
            _script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            _script_dir = os.getcwd()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_dir = os.path.join(_script_dir, "logs", timestamp)
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.log_dir, f"bmc_session_{timestamp}.log")
        self._lock = threading.Lock()
        # buffering=1 = line-buffered; auto-flushes on every \n so the file is
        # safe to read even while the script is still running (or backgrounded).
        self._file = open(self.log_file, "w", encoding="utf-8", buffering=1)
        self._bg_mode = bg_mode
        self._start_time = datetime.now()
        self._phase_times = {}
        self._current_phase = None
        self._current_phase_start = None

        # Per-step timing. Steps are nested-allowed lightweight measurements
        # driven through `step()` (context manager) or start_step/end_step.
        # `_step_times` accumulates total wall time per step name so the
        # summary table can collapse repeated work (e.g. multiple peer SSH
        # connects) into one row.
        self._step_stack = []   # list of dicts: {name, start}
        # Plain dicts are insertion-ordered as of Python 3.7+, which the
        # script requires.  OrderedDict is no longer needed here.
        self._step_times = {}  # name -> total seconds
        self._step_counts = {}  # name -> int

        # Tracks the timestamp of the previous log entry so each line can
        # show the delta since the previous one.
        self._last_log_time = self._start_time

        # Background heartbeat thread + stop event. Started lazily on the
        # first `start_step` so unit tests / quick runs don't pay for it.
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None

        # ---- Outcome tracking ----
        # Counts of ERROR/WARN prefixed log entries (auto-incremented in log()).
        self._error_count = 0
        self._warn_count = 0
        # Collected message text for every WARN / ERROR / FATAL entry so the
        # summary file can list them without requiring a full log scan.
        self._warnings: "list[tuple[str, str]]" = []   # (HH:MM:SS, message)
        self._errors:   "list[tuple[str, str]]" = []   # (HH:MM:SS, message)
        # Explicit overall outcome – set via set_outcome() before close().
        # None means "not yet set; derive at close() time".
        self._final_outcome: "tuple[str, str] | None" = None
        # Operation label set by _make_session_log (e.g. "Mode 2c: resume…").
        self._operation_label: str = ""
        # Per-phase outcomes: phase_name -> (status, note)
        # Phases that call end_phase() without set_phase_outcome() get PASS.
        self._phase_outcomes: "dict[str, tuple[str, str]]" = {}
        # Optional indented sub-timing rows attached under a parent phase
        # in the summary tables. phase_name -> list[(label, elapsed_seconds)].
        self._phase_subtimings: "dict[str, list[tuple[str, float]]]" = {}

        self._write_header()
        print(f"📝 Session log: {self.log_file}")

    # OPT: context-manager support so the file handle is always closed cleanly
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # OPT: centralise timestamp formatting – was duplicated in 5 methods
    def _ts_with_elapsed(self, now=None):
        """Return a timestamp prefix that also includes total elapsed time
        since session start and the delta since the previous log entry. Looks
        like: ``HH:MM:SS.mmm +123.4s Δ0.7s``. Caller must hold the lock.
        """
        now = now or datetime.now()
        total = (now - self._start_time).total_seconds()
        delta = (now - self._last_log_time).total_seconds()
        self._last_log_time = now
        ts = now.strftime("%H:%M:%S.%f")[:-3]
        return f"{ts} +{total:7.1f}s Δ{delta:5.1f}s"

    # ---- Step timing ----------------------------------------------------

    def start_step(self, name):
        """Mark the start of a named step. Multiple steps may be nested;
        each `start_step` must be paired with `end_step`. Returns a token
        that the caller can pass to `end_step` to be safe against mismatched
        nesting; in practice the `step()` context manager is preferred.
        """
        with self._lock:
            now = datetime.now()
            self._step_stack.append({"name": name, "start": now})
            indent = "  " * (len(self._step_stack) - 1)
            self._file.write(
                f"[{self._ts_with_elapsed(now)}] [STEP ▶] {indent}{name}\n"
            )
            self._ensure_heartbeat_locked()
            return len(self._step_stack)

    def end_step(self, name=None):
        """End the most recently started step. If `name` is supplied it is
        validated against the top of the stack and a warning is logged on
        mismatch (so a stray end_step can't silently corrupt the totals).
        """
        with self._lock:
            if not self._step_stack:
                self._file.write(
                    f"[{self._ts_with_elapsed()}] [WARN] end_step called with "
                    f"no active step (name={name!r})\n"
                )
                return
            entry = self._step_stack[-1]
            if name is not None and entry["name"] != name:
                self._file.write(
                    f"[{self._ts_with_elapsed()}] [WARN] end_step name mismatch: "
                    f"expected {entry['name']!r}, got {name!r}\n"
                )
            self._step_stack.pop()
            elapsed = (datetime.now() - entry["start"]).total_seconds()
            self._step_times[entry["name"]] = (
                self._step_times.get(entry["name"], 0.0) + elapsed
            )
            self._step_counts[entry["name"]] = (
                self._step_counts.get(entry["name"], 0) + 1
            )
            indent = "  " * len(self._step_stack)
            self._file.write(
                f"[{self._ts_with_elapsed()}] [STEP ⏹] {indent}{entry['name']} "
                f"({elapsed:.1f}s)\n"
            )

    @contextmanager
    def step(self, name):
        """Context manager: ``with _session_log.step("My Step"): ...``
        Logs start, end, and total duration. Safe across exceptions.
        """
        self.start_step(name)
        try:
            yield
        finally:
            self.end_step(name)

    # ---- Background heartbeat -----------------------------------------

    def _ensure_heartbeat_locked(self):
        """Start the heartbeat thread on first use. Must be called with
        ``self._lock`` held.
        """
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="SessionLogHeartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        """While at least one step is active, periodically log a heartbeat
        line so long-running steps are visible in the log file as time
        progresses. The thread exits when the logger is closed.
        """
        while not self._heartbeat_stop.wait(self._HEARTBEAT_INTERVAL):
            with self._lock:
                if not self._step_stack:
                    continue
                now = datetime.now()
                # Report on the deepest (innermost) active step which is
                # what most callers care about. Outer steps are still
                # being timed and will surface in the summary at close().
                entry = self._step_stack[-1]
                running = (now - entry["start"]).total_seconds()
                indent = "  " * (len(self._step_stack) - 1)
                self._file.write(
                    f"[{self._ts_with_elapsed(now)}] [STEP ⏱] {indent}"
                    f"{entry['name']} still running ({running:.1f}s)\n"
                )

    def _write_header(self):
        self._file.write("=" * 70 + "\n")
        self._file.write("BMC Session Log\n")  # OPT: was an f-string with no interpolation
        self._file.write(f"Started: {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._file.write(f"Host: {platform.node()}\n")
        self._file.write(f"Python: {sys.version}\n")
        self._file.write("=" * 70 + "\n\n")

    def start_phase(self, phase_name):
        with self._lock:
            now = datetime.now()
            if self._current_phase and self._current_phase_start:
                elapsed = (now - self._current_phase_start).total_seconds()
                self._phase_times[self._current_phase] = elapsed
            self._current_phase = phase_name
            self._current_phase_start = now
            self._file.write(
                f"\n[{self._ts_with_elapsed(now)}] [PHASE] ▶ Started: {phase_name}\n"
            )
            self._ensure_heartbeat_locked()

    def end_phase(self, outcome: str = "PASS", note: str = ""):
        """End the current phase.  *outcome* is written into the phase
        outcome table ("PASS", "FAIL", or "PASSED (WITH ERRORS)"). Call
        ``set_phase_outcome()`` *before* ``end_phase()`` to override.
        """
        with self._lock:
            if self._current_phase and self._current_phase_start:
                now = datetime.now()
                elapsed = (now - self._current_phase_start).total_seconds()
                self._phase_times[self._current_phase] = elapsed
                # Record outcome for this phase if not already set explicitly.
                if self._current_phase not in self._phase_outcomes:
                    self._phase_outcomes[self._current_phase] = (outcome, note)
                self._file.write(
                    f"[{self._ts_with_elapsed(now)}] [PHASE] ⏹ Ended: "
                    f"{self._current_phase} ({elapsed:.1f}s) "
                    f"[{self._phase_outcomes[self._current_phase][0]}]\n\n"
                )
                self._current_phase = None
                self._current_phase_start = None

    def set_phase_outcome(self, phase_name: str, status: str, note: str = ""):
        """Explicitly record the outcome of a named phase. Call this before
        ``end_phase()`` if the default (PASS) is not correct.
        """
        with self._lock:
            self._phase_outcomes[phase_name] = (status, note)

    def record_phase(self, phase_name: str, elapsed: float,
                     outcome: str = "PASS", note: str = ""):
        """Record a phase with a precomputed elapsed time, without using the
        ``start_phase``/``end_phase`` wall-clock pair. Useful when the real
        boundaries of a phase are determined inside worker threads and have
        to be reconstructed after the fact (e.g. the mode-3 parallel peer
        add, where option-4 and node-join sub-phases overlap in time).
        """
        with self._lock:
            self._phase_times[phase_name] = float(elapsed)
            self._phase_outcomes[phase_name] = (outcome, note)
            self._file.write(
                f"[{self._ts_with_elapsed()}] [PHASE] ⏹ Recorded: "
                f"{phase_name} ({elapsed:.1f}s) [{outcome}]\n"
            )

    def add_phase_subtiming(self, phase_name: str, label: str, elapsed: float):
        """Attach an indented timing row that will be rendered under
        ``phase_name`` in both the inline and standalone summary tables.
        Safe to call from worker threads.
        """
        with self._lock:
            self._phase_subtimings.setdefault(phase_name, []).append(
                (label, float(elapsed))
            )

    def set_outcome(self, status: str, note: str = ""):
        """Set the overall session outcome ("PASS", "FAIL", or
        "PASSED (WITH ERRORS)"). Should be called before ``close()``.
        """
        with self._lock:
            self._final_outcome = (status, note)

    def record_completion(self, normal_exit: bool = True):
        """Convenience wrapper: sets outcome based on error/warn counters and
        calls close(). Intended to be the single call at the normal exit path
        in main() instead of a manual set_outcome() + close() sequence.
        """
        if normal_exit:
            if self._error_count == 0:
                status, note = "PASS", ""
            else:
                ec, wc = self._error_count, self._warn_count
                note = f"{ec} error(s), {wc} warning(s)" if ec or wc else ""
                status = "PASSED (WITH ERRORS)"
        else:
            status, note = "FAIL", "abnormal exit"
        self.set_outcome(status, note)
        self.close()


    def log(self, message, prefix="INFO"):
        with self._lock:
            upper = prefix.upper()
            now_str = datetime.now().strftime("%H:%M:%S")
            if upper in ("ERROR", "FATAL"):
                self._error_count += 1
                self._errors.append((now_str, message))
            elif upper == "WARN":
                self._warn_count += 1
                self._warnings.append((now_str, message))
            self._file.write(f"[{self._ts_with_elapsed()}] [{prefix}] {message}\n")

    def log_console(self, data):
        with self._lock:
            self._file.write(data)

    def log_user_input(self, data):
        with self._lock:
            self._file.write(f"[{self._ts_with_elapsed()}] [USER_INPUT] {data}\n")

    def log_sent(self, data):
        with self._lock:
            display = repr(data) if any(ord(c) < 32 and c not in '\r\n' for c in data) else data.strip()
            self._file.write(f"[{self._ts_with_elapsed()}] [SENT] {display}\n")

    def close(self):
        # Stop the heartbeat thread BEFORE taking the lock so it can drain
        # any in-flight tick that may already be holding the lock.
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            try:
                self._heartbeat_thread.join(timeout=2.0)
            except RuntimeError:
                pass
        with self._lock:
            # OPT: capture now once instead of calling datetime.now() three times
            now = datetime.now()
            if self._current_phase and self._current_phase_start:
                elapsed = (now - self._current_phase_start).total_seconds()
                self._phase_times[self._current_phase] = elapsed

            # Flush any still-open steps so their partial duration shows up
            # in the summary instead of being silently dropped.
            while self._step_stack:
                entry = self._step_stack.pop()
                elapsed = (now - entry["start"]).total_seconds()
                self._step_times[entry["name"]] = (
                    self._step_times.get(entry["name"], 0.0) + elapsed
                )
                self._step_counts[entry["name"]] = (
                    self._step_counts.get(entry["name"], 0) + 1
                )
                self._file.write(
                    f"[{self._ts_with_elapsed(now)}] [STEP ⏹] {entry['name']} "
                    f"({elapsed:.1f}s, closed at session end)\n"
                )

            total_elapsed = (now - self._start_time).total_seconds()

            # ---- Derive overall outcome ----
            if self._final_outcome is not None:
                outcome_status, outcome_note = self._final_outcome
            elif self._error_count > 0:
                outcome_status = "FAIL"
                outcome_note = f"{self._error_count} error(s) logged"
            elif self._warn_count > 0:
                outcome_status = "PASSED (WITH ERRORS)"
                outcome_note = f"{self._warn_count} warning(s) logged"
            else:
                outcome_status = "PASS"
                outcome_note = ""

            self._file.write(f"\n{'=' * 70}\n")
            self._file.write(f"Session ended: {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._file.write(f"Total runtime: {total_elapsed:.1f}s ({total_elapsed/60:.1f} minutes)\n")

            # ---- Result Summary ----
            self._file.write(f"\n{'─' * 70}\n")
            self._file.write("Result Summary\n")
            self._file.write(f"{'─' * 70}\n")
            if self._operation_label:
                self._file.write(f"  {'Operation':<25} {self._operation_label}\n")
            _status_icon = {"PASS": "✅", "FAIL": "❌", "PASSED (WITH ERRORS)": "⚠️"}.get(outcome_status, "❓")
            self._file.write(f"  {'Overall Result':<25} {_status_icon} {outcome_status}\n")
            if outcome_note:
                self._file.write(f"  {'Detail':<25} {outcome_note}\n")
            self._file.write(f"  {'Errors logged':<25} {self._error_count}\n")
            self._file.write(f"  {'Warnings logged':<25} {self._warn_count}\n")
            self._file.write(f"  {'Started':<25} {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._file.write(f"  {'Ended':<25} {now.strftime('%Y-%m-%d %H:%M:%S')}\n")

            # ---- Phase Timing Summary ----
            self._file.write(f"\n{'─' * 70}\n")
            self._file.write("Phase Timing Summary\n")
            self._file.write(f"{'─' * 70}\n")
            for phase, elapsed in self._phase_times.items():
                minutes = elapsed / 60
                ph_status, ph_note = self._phase_outcomes.get(phase, ("", ""))
                ph_icon = {"PASS": "✅", "FAIL": "❌", "PASSED (WITH ERRORS)": "⚠️"}.get(ph_status, "  ")
                ph_col = f"  {ph_icon} {ph_status}" if ph_status else ""
                self._file.write(
                    f"  {phase:<45} {elapsed:>7.1f}s ({minutes:.1f}m){ph_col}\n"
                )
                for sub_label, sub_elapsed in self._phase_subtimings.get(phase, []):
                    self._file.write(
                        f"     - {sub_label:<41} {sub_elapsed:>7.1f}s "
                        f"({sub_elapsed/60:.1f}m)\n"
                    )
            self._file.write(f"  {'─' * 55}\n")
            self._file.write(f"  {'TOTAL':<45} {total_elapsed:>7.1f}s ({total_elapsed/60:.1f}m)\n")
            # Operator idle time at interactive prompts plus the
            # unaccounted gap (anything not covered by tracked phases or
            # prompt waits) so the wall-clock vs. phase-sum discrepancy
            # is visible at a glance.
            _pw = float(_prompt_wait_seconds)
            _phase_sum = sum(self._phase_times.values()) if self._phase_times else 0.0
            _unaccounted = max(0.0, total_elapsed - _phase_sum - _pw)
            self._file.write(
                f"  {'Time waiting for prompts (x' + str(_prompt_wait_count) + ')':<45} "
                f"{_pw:>7.1f}s ({_pw/60:.1f}m)\n"
            )
            if _prompt_wait_max > 0:
                _lbl = _prompt_wait_max_label or "(no label)"
                self._file.write(
                    f"     - longest single wait: {_prompt_wait_max:.1f}s "
                    f"({_prompt_wait_max/60:.1f}m) at: {_lbl}\n"
                )
            for _ext_lbl, _ext_sec in _prompt_wait_extended:
                self._file.write(
                    f"     - extended wait {_ext_sec:>6.1f}s "
                    f"({_ext_sec/60:.1f}m) at: {_ext_lbl or '(no label)'}\n"
                )
            self._file.write(
                f"  {'Unaccounted time':<45} {_unaccounted:>7.1f}s ({_unaccounted/60:.1f}m)\n"
            )
            if self._step_times:
                self._file.write(f"\n{'─' * 70}\n")
                self._file.write("Step Timing Summary\n")
                self._file.write(f"{'─' * 70}\n")
                for name, elapsed in self._step_times.items():
                    count = self._step_counts.get(name, 1)
                    avg = elapsed / count if count else elapsed
                    label = f"{name} (x{count})" if count > 1 else name
                    self._file.write(
                        f"  {label:<45} {elapsed:>7.1f}s  avg {avg:>5.1f}s\n"
                    )
            self._file.write("=" * 70 + "\n")
            self._file.close()

            # Write a compact summary-only file alongside the full log.
            self._write_summary_file(
                now, total_elapsed,
                outcome_status, outcome_note,
            )

    def _write_summary_file(self, now, total_elapsed, outcome_status, outcome_note):
        """Write a human-readable summary file next to the full session log.

        Contains only the result, phase timings, and step timings — none of
        the raw console output — so it is fast to read after a run.
        """
        summary_path = os.path.join(
            self.log_dir,
            f"summary_{now.strftime('%Y%m%d_%H%M%S')}.log",
        )
        _status_icon = {"PASS": "✅", "FAIL": "❌", "PASSED (WITH ERRORS)": "⚠️"}.get(outcome_status, "❓")
        try:
            with open(summary_path, "w", encoding="utf-8") as sf:
                sf.write("=" * 70 + "\n")
                sf.write("Run Summary\n")
                sf.write(f"Full log: {self.log_file}\n")
                sf.write("=" * 70 + "\n\n")

                # ---- Operation ----
                if self._operation_label:
                    sf.write("─" * 70 + "\n")
                    sf.write("Operation\n")
                    sf.write("─" * 70 + "\n")
                    sf.write(f"  {self._operation_label}\n")

                # ---- Result ----
                sf.write("─" * 70 + "\n")
                sf.write("Result\n")
                sf.write("─" * 70 + "\n")
                sf.write(f"  {'Overall Result':<25} {_status_icon} {outcome_status}\n")
                if outcome_note:
                    sf.write(f"  {'Detail':<25} {outcome_note}\n")
                sf.write(f"  {'Errors logged':<25} {self._error_count}\n")
                sf.write(f"  {'Warnings logged':<25} {self._warn_count}\n")
                sf.write(f"  {'Started':<25} {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                sf.write(f"  {'Ended':<25} {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
                sf.write(f"  {'Total runtime':<25} {total_elapsed:.1f}s ({total_elapsed/60:.1f}m)\n")

                # ---- Phase Timing ----
                if self._phase_times:
                    sf.write("\n" + "─" * 70 + "\n")
                    sf.write("Phase Timing\n")
                    sf.write("─" * 70 + "\n")
                    for phase, elapsed in self._phase_times.items():
                        minutes = elapsed / 60
                        ph_status, _ = self._phase_outcomes.get(phase, ("", ""))
                        ph_icon = {"PASS": "✅", "FAIL": "❌", "PASSED (WITH ERRORS)": "⚠️"}.get(ph_status, "  ")
                        ph_col = f"  {ph_icon} {ph_status}" if ph_status else ""
                        sf.write(f"  {phase:<45} {elapsed:>7.1f}s ({minutes:.1f}m){ph_col}\n")
                        for sub_label, sub_elapsed in self._phase_subtimings.get(phase, []):
                            sf.write(
                                f"     - {sub_label:<41} {sub_elapsed:>7.1f}s "
                                f"({sub_elapsed/60:.1f}m)\n"
                            )
                    sf.write(f"  {'─' * 55}\n")
                    sf.write(f"  {'TOTAL':<45} {total_elapsed:>7.1f}s ({total_elapsed/60:.1f}m)\n")
                    _pw = float(_prompt_wait_seconds)
                    _phase_sum = sum(self._phase_times.values()) if self._phase_times else 0.0
                    _unaccounted = max(0.0, total_elapsed - _phase_sum - _pw)
                    sf.write(
                        f"  {'Time waiting for prompts (x' + str(_prompt_wait_count) + ')':<45} "
                        f"{_pw:>7.1f}s ({_pw/60:.1f}m)\n"
                    )
                    if _prompt_wait_max > 0:
                        _lbl = _prompt_wait_max_label or "(no label)"
                        sf.write(
                            f"     - longest single wait: {_prompt_wait_max:.1f}s "
                            f"({_prompt_wait_max/60:.1f}m) at: {_lbl}\n"
                        )
                    for _ext_lbl, _ext_sec in _prompt_wait_extended:
                        sf.write(
                            f"     - extended wait {_ext_sec:>6.1f}s "
                            f"({_ext_sec/60:.1f}m) at: {_ext_lbl or '(no label)'}\n"
                        )
                    sf.write(
                        f"  {'Unaccounted time':<45} {_unaccounted:>7.1f}s ({_unaccounted/60:.1f}m)\n"
                    )

                # ---- Step Timing ----
                if self._step_times:
                    sf.write("\n" + "─" * 70 + "\n")
                    sf.write("Step Timing\n")
                    sf.write("─" * 70 + "\n")
                    for name, elapsed in self._step_times.items():
                        count = self._step_counts.get(name, 1)
                        avg = elapsed / count if count else elapsed
                        label = f"{name} (x{count})" if count > 1 else name
                        sf.write(f"  {label:<45} {elapsed:>7.1f}s  avg {avg:>5.1f}s\n")

                # ---- Warnings ----
                if self._warnings:
                    sf.write("\n" + "─" * 70 + "\n")
                    sf.write(f"Warnings ({len(self._warnings)})\n")
                    sf.write("─" * 70 + "\n")
                    for ts, msg in self._warnings:
                        sf.write(f"  [{ts}] {msg}\n")

                # ---- Errors ----
                if self._errors:
                    sf.write("\n" + "─" * 70 + "\n")
                    sf.write(f"Errors ({len(self._errors)})\n")
                    sf.write("─" * 70 + "\n")
                    for ts, msg in self._errors:
                        sf.write(f"  [{ts}] {msg}\n")

                sf.write("\n" + "=" * 70 + "\n")

            print(f"📋 Summary log: {summary_path}")
        except OSError as e:
            # Non-fatal — full log is still written.
            print(f"  ⚠️  Could not write summary log: {e}")


_session_log = None


def _slog(msg, prefix="INFO"):
    """Shorthand: write *msg* to the active session log (no-op if none set)."""
    if _session_log:
        _session_log.log(msg, prefix=prefix)


# ---------------------------------------------------------------------------
# Operation mode selection
# ---------------------------------------------------------------------------

_operation_mode = None
# When True (1b / 3 primary), the script auto-answers all post-option-9
# prompts and drives the cluster setup wizard non-interactively.
_auto_setup = False
# When True (2b / 3 peers), the script auto-drives the join wizard for an
# existing cluster after option 4.
_auto_add = False


def select_operation_mode():
    """Return (operation_mode_int, auto_setup_bool, auto_add_bool).

    Modes:
      1  -> Initial cluster creation
        1a: format first node, interactive (auto_setup=False)
        1b: format first node, automatic   (auto_setup=True)
      2  -> Add new nodes to an existing cluster
        2a: format new node, interactive   (auto_add=False)
        2b: format new node, automatic     (auto_add=True)
      3  -> End-to-end auto initialize: 1b on primary + parallel auto-add
            for every other BMC discovered/entered.
      4  -> Install/manage ONTAP  (sub-menu)
        4a: Install ONTAP from cluster shell          (not yet implemented)
        4b: Netboot and install ONTAP                 (not yet implemented)
        4c: Install license file only                 (44 internally)
      5  -> Exit.
    """
    global _setup_passwordless_ssh, _netboot_before_reinit, _physical_zeroing
    while True:
        _print_banner("NetApp AFX BMC Console Automation 🤖")
        print("\n  What do you want to do?\n")
        print("  1.  Initial cluster creation")
        print("    1a. Format first node in cluster. Use interactive configuration.")
        print("    1b. Format first node in cluster. Use automatic configuration.")
        print("")
        print("  2.  Add new nodes")
        print("    2a. Format new nodes. Use interactive node add.")
        print("    2b. Format new nodes. Use automatic add.")
        print("    2c. Resume interrupted node additions.")
        print("")
        print("  3.  End to end auto initialize")
        print("  4.  Install/manage ONTAP")
        print("    4a. Upgrade ONTAP (rolling takeover/giveback)")
        print("    4b. Netboot and install ONTAP")
        print("    4c. Install license file only")
        print("    4d. Set up passwordless SSH to cluster management")
        print("    4e. Create backup cluster configuration")
        print("    4f. Verify BMC authentication")
        print("  5.  Exit")
        print("")
        print("  " + "─" * 58)
        choice = input("  Enter your choice (1a, 1b, 2a, 2b, 2c, 3, 4a-4f, or 5): ").strip().lower()

        if choice == "1a":
            _print_banner("⚠️  WARNING ⚠️")
            print("")
            print("  You will be destroying the storage availability zone on")
            print("  this cluster, deleting all data and reinitializing the")
            print("  entire cluster.")
            print("")
            print("  " + "*" * 58)
            print("  * CAUTION: IF THIS IS NOT THE FIRST NODE IN THE        *")
            print("  * CLUSTER DO NOT RUN THIS OPTION. RUN OPTION 2         *")
            print("  * INSTEAD TO JOIN A NEW NODE TO THE CLUSTER.            *")
            print("  " + "*" * 58)
            print("")
            print("  " + "─" * 58)
            confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
            if confirm == "yes":
                _ssh_ans = input("  Set up passwordless SSH to cluster management after setup? [y/N]: ").strip().lower()
                _setup_passwordless_ssh = (_ssh_ans == "y")
                _nb_ans = input("  Do you want to install a specific version of ONTAP before re-creating the cluster? [y/N]: ").strip().lower()
                _netboot_before_reinit = (_nb_ans == "y")
                if _netboot_before_reinit:
                    print("  ℹ️   Netboot-install will run at LOADER before the cluster reinit.")
                print("  ℹ️   Physical zeroing can help ensure consistency in throughput results.")
                _pz_ans = input("  Do you want to physically zero all disks? (This can add time to the reinit process) [y/N]: ").strip().lower()
                _physical_zeroing = (_pz_ans == "y")
                if _physical_zeroing:
                    print("  ℹ️   Physical disk zeroing enabled (raid.use-physical-zeroing).")
                print("\n  ✅ Confirmed. 1a: Format first node (interactive)")
                print("     → LOADER: set-defaults + destroy storage pods + saveenv")
                print("     → Boot menu: option 9 (Initialize); then interactive\n")
                return 1, False, False
            print("\n  ↩️  Returning to menu...\n")
            continue

        if choice == "1b":
            _print_banner("⚠️  WARNING ⚠️")
            print("")
            print("  1b will FULLY AUTOMATE first-node initialization, format,")
            print("  and cluster setup. The script will auto-answer:")
            print("    • storage-availability-zone destroy warning  → no")
            print("    • second boot menu (after option 9)          → 4")
            print("    • zero disks / erase / type-yes prompts      → yes")
            print("  Node management port/IP/netmask/gateway are taken from")
            print("  retained config when available, prompted otherwise.")
            print("")
            print("  " + "*" * 58)
            print("  * CAUTION: IF THIS IS NOT THE FIRST NODE IN THE        *")
            print("  * CLUSTER DO NOT RUN THIS OPTION. RUN OPTION 2         *")
            print("  * INSTEAD TO JOIN A NEW NODE TO THE CLUSTER.            *")
            print("  " + "*" * 58)
            print("")
            print("  " + "─" * 58)
            confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
            if confirm == "yes":
                _ssh_ans = input("  Set up passwordless SSH to cluster management after setup? [y/N]: ").strip().lower()
                _setup_passwordless_ssh = (_ssh_ans == "y")
                _nb_ans = input("  Do you want to install a specific version of ONTAP before re-creating the cluster? [y/N]: ").strip().lower()
                _netboot_before_reinit = (_nb_ans == "y")
                if _netboot_before_reinit:
                    print("  ℹ️   Netboot-install will run at LOADER before the cluster reinit.")
                print("  ℹ️   Physical zeroing can help ensure consistency in throughput results.")
                _pz_ans = input("  Do you want to physically zero all disks? (This can add time to the reinit process) [y/N]: ").strip().lower()
                _physical_zeroing = (_pz_ans == "y")
                if _physical_zeroing:
                    print("  ℹ️   Physical disk zeroing enabled (raid.use-physical-zeroing).")
                print("\n  ✅ Confirmed. 1b: Format first node + setup cluster (auto)")
                print("     → LOADER: set-defaults + destroy storage pods + saveenv")
                print("     → Boot menu: option 9, then auto option 4 + auto setup\n")
                return 1, True, False
            print("\n  ↩️  Returning to menu...\n")
            continue

        if choice == "2a":
            _print_banner("⚠️  NOTICE ⚠️")
            print("")
            print("  " + "*" * 58)
            print("  * CAUTION: 2a FORMATS AND JOINS AN AFX NODE TO AN     *")
            print("  * EXISTING CLUSTER. IF THE CLUSTER DOES NOT EXIST     *")
            print("  * ALREADY, CHOOSE NO AND SELECT OPTION 1a OR 1b.       *")
            print("  " + "*" * 58)
            print("")
            print("  " + "─" * 58)
            confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
            if confirm == "yes":
                _nb_ans = input("  Do you want to install a specific version of ONTAP before adding this node? [y/N]: ").strip().lower()
                _netboot_before_reinit = (_nb_ans == "y")
                if _netboot_before_reinit:
                    print("  ℹ️   Netboot-install will run at LOADER before the node join.")
                print("\n  ✅ Confirmed. 2a: Add node (interactive)")
                print("     → LOADER: set-defaults + saveenv (no destroy storage pods)")
                print("     → Boot menu: option 4 (Initialize and configure system)\n")
                return 2, False, False
            print("\n  ↩️  Returning to menu...\n")
            continue

        if choice == "2b":
            _print_banner("⚠️  NOTICE ⚠️")
            print("")
            print("  2b will FULLY AUTOMATE adding a node to an existing")
            print("  cluster. The script auto-answers zero/erase/yes prompts,")
            print("  populates node-management info from the config or prompts,")
            print("  and answers 'join' at the create/join step.")
            print("")
            print("  " + "*" * 58)
            print("  * CAUTION: 2b REQUIRES AN EXISTING CLUSTER. IF NONE    *")
            print("  * EXISTS, USE 1a OR 1b INSTEAD.                        *")
            print("  " + "*" * 58)
            print("")
            print("  " + "─" * 58)
            confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
            if confirm == "yes":
                _nb_ans = input("  Do you want to install a specific version of ONTAP before adding this node? [y/N]: ").strip().lower()
                _netboot_before_reinit = (_nb_ans == "y")
                if _netboot_before_reinit:
                    print("  ℹ️   Netboot-install will run at LOADER before the node join.")
                print("\n  ✅ Confirmed. 2b: Add node (auto)")
                print("     → LOADER: set-defaults + saveenv")
                print("     → Boot menu: option 4 + auto join wizard\n")
                return 2, False, True
            print("\n  ↩️  Returning to menu...\n")
            continue

        if choice == "2c":
            print("\n  ✅ Confirmed. 2c: Resume node additions\n")
            return 26, False, False

        if choice == "3":
            _print_banner("⚠️  WARNING ⚠️")
            print("")
            print("  Option 3: End-to-end auto initialize.")
            print("    1) Format + setup the FIRST node automatically (1b).")
            print("    2) Once the cluster is created, format and JOIN every")
            print("       additional BMC in PARALLEL. The 'create or join'")
            print("       step is serialized so each node is fully added")
            print("       (verified via 'cluster show') before the next one.")
            print("")
            print("  " + "*" * 58)
            print("  * THIS DESTROYS ALL DATA ON ALL TARGETED NODES.        *")
            print("  " + "*" * 58)
            print("")
            print("  " + "─" * 58)
            confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
            if confirm == "yes":
                _ssh_ans = input("  Set up passwordless SSH to cluster management after setup? [y/N]: ").strip().lower()
                _setup_passwordless_ssh = (_ssh_ans == "y")
                _nb_ans = input("  Do you want to install a specific version of ONTAP on all nodes first? [y/N]: ").strip().lower()
                _netboot_before_reinit = (_nb_ans == "y")
                if _netboot_before_reinit:
                    print("  ℹ️   Netboot-install will run at LOADER on the primary node before cluster reinit.")
                print("  ℹ️   Physical zeroing can help ensure consistency in throughput results.")
                _pz_ans = input("  Do you want to physically zero all disks? (This can add time to the reinit process) [y/N]: ").strip().lower()
                _physical_zeroing = (_pz_ans == "y")
                if _physical_zeroing:
                    print("  ℹ️   Physical disk zeroing enabled (raid.use-physical-zeroing).")
                print("\n  ✅ Confirmed. 3: End-to-end auto initialize\n")
                return 3, True, True
            print("\n  ↩️  Returning to menu...\n")
            continue

        if choice in ("4", "4a", "4b", "4c", "4d", "4e", "4f"):
            if choice == "4":
                # Show the sub-menu and re-prompt.
                _print_banner("\U0001f4e6 Install/Manage ONTAP")
                print("\n  4a. Upgrade ONTAP (rolling takeover/giveback)")
                print("  4b. Netboot and install ONTAP")
                print("  4c. Install license file only")
                print("  4d. Set up passwordless SSH to cluster management")
                print("  4e. Create backup cluster configuration")
                print("  4f. Verify BMC authentication")
                print("")
                print("  " + "─" * 58)
                choice = input("  Enter sub-option (4a, 4b, 4c, 4d, 4e, 4f) or blank to go back: ").strip().lower()
                if not choice:
                    continue

            if choice == "4a":
                _print_banner("\U0001f4e6 4a: Upgrade ONTAP")
                print("")
                print("  Performs a rolling upgrade via storage failover")
                print("  takeover/giveback. Only upgrades are supported.")
                print("  Provide a .tgz package or a URL.")
                print("")
                print("  " + "\u2500" * 58)
                confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
                if confirm == "yes":
                    print("\n  \u2705 Confirmed. 4a: ONTAP upgrade\n")
                    return 41, False, False
                print("\n  \u21a9\ufe0f  Returning to menu...\n")
                continue

            if choice == "4b":
                # 4b: Netboot and install ONTAP
                _print_banner("\U0001f4e6 4b: Netboot and install ONTAP")
                print("")
                print("  You are about to netboot the nodes in this cluster.")
                print("  This is intended for use with new or reinitializing")
                print("  clusters and requires each node to be rebooted into")
                print("  the LOADER prompt, which constitutes an outage.")
                print("")
                print("  If you have an existing cluster you wish to upgrade")
                print("  without taking the nodes down, use option 4a instead.")
                print("")
                print("  " + "\u2500" * 58)
                confirm = input("  Continue with netboot? [y/N]: ").strip().lower()
                if confirm == "y":
                    print("\n  \u2705 Confirmed. 4b: Netboot and install ONTAP\n")
                    return 42, False, False
                print("\n  \u21a9\ufe0f  Returning to menu...\n")
                continue

            if choice == "4c":
                _print_banner("\U0001f4dc 4c: Install license file only")
                print("")
                print("  Connects to the BMC, enters the system console, logs in")
                print("  to the cluster shell, and applies a pre-staged license")
                print("  file (or license keys) without running any reinit steps.")
                print("")
                print("  " + "─" * 58)
                confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
                if confirm == "yes":
                    print("\n  \u2705 Confirmed. 4c: Install license file only\n")
                    return 44, False, False
                print("\n  \u21a9\ufe0f  Returning to menu...\n")
                continue

            if choice == "4d":
                _print_banner("\U0001f511 4d: Set up passwordless SSH to cluster management")
                print("")
                print("  Generates an RSA-4096 key pair on this host (if needed),")
                print("  then configures the cluster to accept public-key login")
                print("  for the specified user.")
                print("")
                print("  " + "─" * 58)
                confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
                if confirm == "yes":
                    print("\n  \u2705 Confirmed. 4d: Set up passwordless SSH\n")
                    return 45, False, False
                print("\n  \u21a9\ufe0f  Returning to menu...\n")
                continue

            if choice == "4e":
                _print_banner("\U0001f4be 4e: Create backup cluster configuration")
                print("")
                print("  Connects to the primary node BMC and reads the current")
                print("  cluster configuration, then writes it to a local")
                print("  reinit-config.json snapshot file for future use.")
                print("")
                print("  " + "─" * 58)
                confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
                if confirm == "yes":
                    print("\n  \u2705 Confirmed. 4e: Create backup cluster configuration\n")
                    return 46, False, False
                print("\n  \u21a9\ufe0f  Returning to menu...\n")

            if choice == "4f":
                _print_banner("\U0001f50d 4f: Verify BMC authentication")
                print("")
                print("  Loads BMC IP addresses from BMC_IP.json (or prompts),")
                print("  attempts SSH login to each BMC, runs 'bmc status', and")
                print("  reports PASS/FAIL per node.")
                print("")
                print("  " + "─" * 58)
                confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
                if confirm == "yes":
                    print("\n  \u2705 Confirmed. 4f: Verify BMC authentication\n")
                    return 47, False, False
                print("\n  \u21a9\ufe0f  Returning to menu...\n")
            continue

        if choice == "5":
            print("\n  \U0001f44b Exiting script. No changes were made.")
            sys.exit(0)

        print("  \u26a0\ufe0f  Invalid choice. Please enter 1a, 1b, 2a, 2b, 3, 4a-4f, or 5.")


def get_loader_commands():
    # Mode 3 primary uses mode-1 LOADER commands (full cluster init); peers in
    # mode 3 are driven by their own threads with their own LOADER commands.
    if _operation_mode in (1, 3):
        cmds = [
            "set-defaults",
            "setenv AUTO_FW_UPDATE false",
            "setenv bootarg.destroy.all.storage.pods true",
        ]
        if _physical_zeroing:
            cmds.append("setenv raid.use-physical-zeroing? true")
        cmds += ["saveenv", "boot_ontap menu"]
        return cmds
    else:
        cmds = [
            "set-defaults",
            "setenv AUTO_FW_UPDATE false",
        ]
        # Physical zeroing is a per-node LOADER setting; apply it to
        # peers too when the operator opted in so the whole cluster ends
        # up with consistent raid.use-physical-zeroing? behaviour.
        if _physical_zeroing:
            cmds.append("setenv raid.use-physical-zeroing? true")
        cmds += ["saveenv", "boot_ontap menu"]
        return cmds


def get_boot_menu_option():
    if _operation_mode in (1, 3):
        return "9", "Initialize"
    else:
        return "4", "Initialize and configure system"


# ---------------------------------------------------------------------------
# OS / Package-manager detection
# ---------------------------------------------------------------------------

def detect_package_manager():
    if sys.platform != "linux":
        return None
    try:
        with open("/etc/os-release") as f:
            os_release = f.read().lower()
    except FileNotFoundError:
        os_release = ""
    if "ubuntu" in os_release or "debian" in os_release:
        return "apt"
    elif "rhel" in os_release or "centos" in os_release or "fedora" in os_release or "red hat" in os_release:
        if os.path.exists("/usr/bin/dnf"):
            return "dnf"
        elif os.path.exists("/usr/bin/yum"):
            return "yum"
    return None


def install_system_package(package_name, pkg_manager):
    answer = input(
        f"⚠️  System package '{package_name}' is required but not installed.\n"
        f"   Install it now using '{pkg_manager}'? [Y/N]: "
    ).strip().lower()
    if answer != "y":
        print("❌ Cannot continue without the required package. Exiting.")
        sys.exit(1)
    try:
        cmd = ["sudo", pkg_manager, "install", "-y", package_name]
        print(f"Running: {' '.join(cmd)}")
        subprocess.check_call(cmd)
        print(f"✅ '{package_name}' installed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install '{package_name}': {e}")
        sys.exit(1)


REQUIRED_MODULES = {
    "paramiko": {
        "pip": "paramiko",
        "apt": "python3-paramiko",
        "dnf": "python3-paramiko",
        "yum": "python3-paramiko",
    },
}


def install_required_modules():
    pkg_manager = detect_package_manager()
    for module_name, pkg_info in REQUIRED_MODULES.items():
        try:
            # OPT: importlib.import_module is the modern API for dynamic imports
            importlib.import_module(module_name)
            continue
        except ImportError:
            pass
        if pkg_manager and pkg_manager in pkg_info:
            print(f"Module '{module_name}' is missing.")
            install_system_package(pkg_info[pkg_manager], pkg_manager)
            try:
                importlib.import_module(module_name)
                continue
            except ImportError:
                print("⚠️  System package installed but module still not importable. "
                      "Falling back to pip.")
        answer = input(
            f"⚠️  Python module '{module_name}' is not installed.\n"
            f"   Install it now via pip? [Y/N]: "
        ).strip().lower()
        if answer != "y":
            print("❌ Cannot continue without the required module. Exiting.")
            sys.exit(1)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--upgrade",
                 "pip", "setuptools", "wheel"]
            )
        except subprocess.CalledProcessError:
            pass
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg_info["pip"]]
            )
            print(f"✅ Module '{module_name}' installed successfully via pip.")
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to install module '{module_name}': {e}")
            sys.exit(1)


install_required_modules()

import paramiko  # noqa: E402

# Quiet paramiko's own logger. It emits noisy ERROR-level tracebacks for
# transient failures we already handle (banner timeouts, EOF during
# negotiation, etc.). The retry loop in `_ssh_connect_with_retry` surfaces
# the same condition through its own user-friendly print, so suppressing
# paramiko's internal logging keeps the console clean.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

# When True (set by 1a/1b "install specific ONTAP?" prompt), netboot-install
# is performed at LOADER before the normal boot-menu/reinit flow continues.
_netboot_before_reinit = False

# When True (set by 1a/1b/3 prompt), adds 'setenv raid.use-physical-zeroing? true'
# to the primary node's LOADER commands so disks are physically zeroed.
_physical_zeroing = False

_shutdown_event = threading.Event()
_client_lock    = threading.Lock()
_active_client  = None
_ctrl_c_count   = 0
_bg_mode        = False  # True when --bg flag is passed

# Primary BMC credentials currently in use. Populated by `connect_to_sp` so
# subsequent helpers (cluster login, etc.) can fall back to BMC creds when
# no cluster admin credentials are available.
_primary_bmc_user = None
_primary_bmc_password = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(debug: bool):
    if debug:
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        logging.getLogger("paramiko").setLevel(logging.DEBUG)
        print("🐛 Debug logging is ENABLED.")
    else:
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger("paramiko").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _silent_ping(host):
    """Return True if `host` responds to a single ICMP ping, False otherwise."""
    import subprocess as _sp
    import platform as _pl
    _cmd = (["ping", "-n", "1", "-w", "2000", host]
            if _pl.system().lower() == "windows"
            else ["ping", "-c", "1", "-W", "2", host])
    try:
        return _sp.run(_cmd, capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _check_bmc_reachable(host):
    import socket as _sock
    import subprocess as _sp

    ok = True

    # DNS check (only for non-IP hostnames).
    _is_ip = False
    try:
        _sock.inet_pton(_sock.AF_INET, host)
        _is_ip = True
    except OSError:
        try:
            _sock.inet_pton(_sock.AF_INET6, host)
            _is_ip = True
        except OSError:
            pass

    if not _is_ip:
        try:
            resolved = _sock.getaddrinfo(host, None)[0][4][0]
            print(f"  ✅ DNS resolved: {host} → {resolved}")
        except _sock.gaierror as _e:
            print(f"  ⚠️  DNS lookup failed for '{host}': {_e}")
            ok = False

    # Ping check (one packet, 2-second timeout).
    import platform as _pl
    _ping_cmd = (["ping", "-n", "1", "-w", "2000", host]
                 if _pl.system().lower() == "windows"
                 else ["ping", "-c", "1", "-W", "2", host])
    try:
        _result = _sp.run(_ping_cmd, capture_output=True, timeout=5)
        if _result.returncode == 0:
            print(f"  ✅ Ping OK: {host} is reachable.")
        else:
            print(f"  ⚠️  Ping failed: {host} did not respond.")
            ok = False
    except Exception as _pe:
        print(f"  ⚠️  Ping check skipped: {_pe}")
        # Don't mark as failed — ping may be blocked by firewall.

    return ok


def _prompt_bmc_host(prompt_text="  BMC hostname/IP: ", allow_blank=False):
    """Prompt for a BMC hostname/IP and re-prompt until it is reachable.

    Calls :func:`_check_bmc_reachable` after each entry; on failure the user
    is asked again. Blank input re-prompts unless ``allow_blank`` is True,
    in which case an empty string is returned so the caller can treat blank
    as a cancel sentinel.
    """
    while True:
        host = input(prompt_text).strip()
        if not host:
            if allow_blank:
                return ""
            print("  ⚠️  A BMC hostname or IP address is required.")
            continue
        if _check_bmc_reachable(host):
            return host
        print("  ⚠️  Please enter a valid, reachable BMC hostname or IP address.\n")


def configure_transport(client):
    transport = client.get_transport()
    if transport is None:
        return
    transport.set_keepalive(5)
    sock = transport.sock
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 12)
        except OSError:
            pass


# ── Stale BMC SSH session helpers ────────────────────────────────────────
# These are called by the banner-retry block in _ssh_connect_with_retry to
# free up BMC SSH session slots before the next retry attempt.

def _register_bmc_client(host: str, client) -> None:
    """Remember `client` as an open SSH client to `host` so the banner-retry
    helper can forcibly close it later if the BMC's session pool stalls."""
    if not host or client is None:
        return
    with _bmc_clients_lock:
        _bmc_clients_by_host.setdefault(host, []).append(client)


def _unregister_bmc_client(host: str, client) -> None:
    """Drop `client` from the registry (call from explicit close paths)."""
    if not host or client is None:
        return
    with _bmc_clients_lock:
        lst = _bmc_clients_by_host.get(host)
        if not lst:
            return
        with suppress(ValueError):
            lst.remove(client)
        if not lst:
            _bmc_clients_by_host.pop(host, None)


def _drop_bmc_clients_for(host: str, log=None) -> int:
    """Force-close every registered paramiko SSHClient for `host`.

    Returns the count of clients dropped. The transport is closed rudely
    (no SSH_MSG_DISCONNECT) so a hung remote sshd cannot block us. Safe to
    call concurrently with other threads using those clients — they will
    see EOF and surface their own errors, which the banner retry logic
    already handles.
    """
    with _bmc_clients_lock:
        clients = _bmc_clients_by_host.pop(host, [])
    if not clients:
        return 0
    for c in clients:
        with suppress(Exception):
            t = c.get_transport()
            if t is not None:
                t.close()
        with suppress(Exception):
            c.close()
    if log is not None:
        with suppress(Exception):
            log.log(
                f"[{host}] banner-retry: closed {len(clients)} registered "
                "in-process SSH client(s) to free BMC session slot",
                prefix="WARN",
            )
    return len(clients)


def _find_stale_sockets_to(host: str, port: int = 22):
    """Return a list of (pid, proc_name, laddr) tuples for ESTABLISHED TCP
    sockets from this host to `host`:`port`. Requires psutil; returns []
    if psutil is unavailable or the lookup fails.
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    out = []
    with suppress(Exception):
        for c in psutil.net_connections(kind="tcp"):
            try:
                if (c.status == "ESTABLISHED"
                        and c.raddr
                        and c.raddr.ip == host
                        and c.raddr.port == port):
                    pname = ""
                    if c.pid:
                        with suppress(Exception):
                            pname = psutil.Process(c.pid).name()
                    out.append((c.pid, pname, c.laddr))
            except Exception:
                continue
    return out


def _kill_stale_local_pids_for(host: str, log=None) -> int:
    """Find ESTABLISHED TCP sockets from this host to <host>:22 owned by
    *other* python processes (likely prior AFX_reinit runs that died
    without cleaning up) and SIGTERM them.

    Skips our own PID (those are handled by _drop_bmc_clients_for) and
    non-python processes (sshd, scp, the operator's interactive ssh, etc.)
    to avoid harming unrelated work.

    Returns the count of PIDs signalled. No-op if psutil unavailable.
    """
    socks = _find_stale_sockets_to(host, 22)
    if not socks:
        return 0
    our_pid = os.getpid()
    seen: "set[int]" = set()
    killed = 0
    for pid, pname, _laddr in socks:
        if not pid or pid == our_pid or pid in seen:
            continue
        seen.add(pid)
        # Only act on python processes — these are the realistic candidates
        # for a stale prior run of this script. Other binaries are left
        # alone so we don't disrupt the operator's own SSH or unrelated
        # automation.
        if "python" not in (pname or "").lower():
            if log is not None:
                with suppress(Exception):
                    log.log(
                        f"[{host}] banner-retry: stale socket from "
                        f"pid={pid} ({pname or 'unknown'}); leaving alone "
                        "(not a python process)",
                    )
            continue
        with suppress(Exception):
            print(
                f"   🧹 [{host}] sending SIGTERM to stale pid={pid} "
                f"({pname}) holding an SSH session to the BMC..."
            )
        if log is not None:
            with suppress(Exception):
                log.log(
                    f"[{host}] banner-retry: SIGTERM pid={pid} ({pname}) "
                    "(stale SSH socket to BMC)",
                    prefix="WARN",
                )
        try:
            if hasattr(signal, "SIGTERM"):
                os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            with suppress(Exception):
                print(
                    f"   ⚠️  [{host}] no permission to signal pid={pid}; "
                    "skipping."
                )
        except Exception as _kex:
            with suppress(Exception):
                print(f"   ⚠️  [{host}] could not signal pid={pid}: {_kex}")
    return killed


def _ipmi_sol_deactivate(host: str, username: str, password: str,
                         log=None) -> bool:
    """Call `ipmitool sol deactivate` to free any stuck SOL session on the
    BMC. Returns True if ipmitool ran (regardless of exit code), False if
    ipmitool is not available or could not be invoked. Best-effort; any
    error is swallowed.
    """
    ipmi_bin = shutil.which("ipmitool")
    if not ipmi_bin:
        return False
    if not host or not username or password is None:
        return False
    cmd = [
        ipmi_bin, "-I", "lanplus",
        "-H", host, "-U", username, "-P", password,
        "sol", "deactivate",
    ]
    try:
        # Short timeout — if ipmitool hangs the BMC is unreachable anyway.
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
        rc = result.returncode
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
        if log is not None:
            with suppress(Exception):
                log.log(
                    f"[{host}] banner-retry: ipmitool sol deactivate "
                    f"rc={rc} ({tail[0][:120]})",
                )
        with suppress(Exception):
            if rc == 0:
                print(f"   🧹 [{host}] ipmitool: SOL session deactivated.")
            else:
                # rc != 0 is common (no session to deactivate); just log.
                print(
                    f"   ℹ️  [{host}] ipmitool sol deactivate rc={rc} "
                    "(likely no active SOL session)."
                )
        return True
    except subprocess.TimeoutExpired:
        if log is not None:
            with suppress(Exception):
                log.log(
                    f"[{host}] banner-retry: ipmitool sol deactivate timed out",
                    prefix="WARN",
                )
        return True
    except Exception as _iex:
        if log is not None:
            with suppress(Exception):
                log.log(
                    f"[{host}] banner-retry: ipmitool sol deactivate failed: {_iex}",
                    prefix="WARN",
                )
        return False


def _clear_stale_bmc_sessions(host: str, username: str, password: str,
                              log=None) -> None:
    """Best-effort sweep called from the banner-retry block before each
    retry sleep. Always closes in-process SSH clients to `host` (safe).
    When `--auto-clear-stale-bmc` is set, additionally scans for stale
    local PIDs and runs `ipmitool sol deactivate`.
    """
    # (1) Always safe: drop our own registered clients to this host.
    _drop_bmc_clients_for(host, log=log)
    # (2) Always safe: ipmitool sol deactivate. Cheap; clears a frequent
    # offender (a hung SOL session keeping the BMC's session pool full).
    with suppress(Exception):
        _ipmi_sol_deactivate(host, username, password, log=log)
    # (3) Opt-in only: signal stale prior-run python PIDs that hold a TCP
    # socket to <host>:22. Skipped by default because it can kill the
    # operator's own concurrent script invocation.
    if _auto_clear_stale_bmc:
        with suppress(Exception):
            socks = _find_stale_sockets_to(host, 22)
            if socks:
                with suppress(Exception):
                    print(
                        f"   🔍 [{host}] found {len(socks)} local TCP "
                        "socket(s) to BMC:22; checking for stale prior runs..."
                    )
            _kill_stale_local_pids_for(host, log=log)


def _ssh_connect_with_retry(host: str, username: str, password: str,
                            label: str = "BMC",
                            max_attempts: int = 5,
                            interactive: bool = True,
                            fallback_passwords: "list[str] | None" = None
                            ) -> "tuple[paramiko.SSHClient, str, str]":
    """Open an SSH client to `host` with retry-on-auth-failure.

    On `paramiko.AuthenticationException` (or any failure mentioning
    "auth"), the function first silently tries each password in
    `fallback_passwords` (if provided) before prompting the operator.
    If all fallbacks and the interactive prompt are exhausted the last
    exception is re-raised so the caller can decide to abort.

    `interactive=False` disables the credential re-prompt — useful from
    background threads where stdin contention would be problematic. In
    that mode the function only retries transient (non-auth) failures.
    """
    # Build the ordered list of (username, password) pairs to attempt.
    # Start with the supplied creds, then silently try any fallbacks.
    _attempt_queue = [(username, password)]
    for _fb in (fallback_passwords or []):
        if _fb is not None and (username, _fb) not in _attempt_queue:
            _attempt_queue.append((username, _fb))
    _queue_idx = 0  # index into _attempt_queue for silent fallback phase

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        # Pick creds: work through the silent fallback queue first, then
        # use whatever (username, password) were last set by the prompt.
        if _queue_idx < len(_attempt_queue):
            username, password = _attempt_queue[_queue_idx]
            _queue_idx += 1
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            _silent = _queue_idx <= len(_attempt_queue) and _queue_idx > 1
            if not _silent:
                print(f"   🔌 [{label}] connecting to {host} as {username} "
                      f"(attempt {attempt}/{max_attempts})...")
            else:
                print(f"   🔌 [{label}] trying fallback credentials for {host} "
                      f"(attempt {attempt}/{max_attempts})...")
            if _session_log:
                _session_log.log(
                    f"[{label}] SSH connect to {host} as {username} "
                    f"(attempt {attempt}/{max_attempts})"
                )
            client.connect(hostname=host, username=username, password=password,
                            timeout=45, banner_timeout=60, auth_timeout=45,
                            disabled_algorithms={"pubkeys": ["ssh-dss"]})
            configure_transport(client)
            _register_bmc_client(host, client)
            return client, username, password
        except paramiko.AuthenticationException as e:
            last_exc = e
            print(f"   ❌ [{label}] authentication failed for {username}@{host}.")
            if _session_log:
                _session_log.log(
                    f"[{label}] auth failed for {username}@{host}",
                    prefix="ERROR",
                )
            # If we still have silent fallbacks to try, loop immediately.
            if _queue_idx < len(_attempt_queue):
                continue
            if not interactive or attempt >= max_attempts:
                break
            try:
                with _stdin_lock:
                    print(f"\n   \U0001F510 Re-enter credentials for {host} "
                          f"(was: {username})")
                    new_user = input(
                        f"   Enter username for {host} [{username}] (blank to keep): "
                    ).strip() or username
                    new_pass = getpass.getpass(
                        f"   Enter password for {new_user}@{host}: "
                    )
            except (EOFError, KeyboardInterrupt):
                break
            if not new_pass:
                print("   ⚠️  Empty password; aborting retry.")
                break
            username, password = new_user, new_pass
            if _session_log:
                _session_log.log(
                    f"[{label}] retrying with new credentials for {host} "
                    f"(user={username})"
                )
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            # ── BMC SSH temporarily not responding ────────────────────
            # When the BMC briefly drops SSH (post-reboot, BMC service
            # restart, transient network blip) paramiko surfaces
            # "Connection reset by peer" or a ConnectionResetError. In
            # those cases the BMC usually recovers in 1–5 minutes, so
            # notify the operator and retry every 2 minutes for up to
            # 10 minutes (5 retries) before giving up entirely.
            if (isinstance(e, ConnectionResetError)
                    or "connection reset" in msg
                    or "reset by peer" in msg):
                _reset_max = 5
                _reset_interval = 120  # seconds
                print(
                    f"\n   ⚠️  [{label}] SSH to {host} is currently not "
                    f"responding (connection reset by peer)."
                )
                print(
                    f"      Will retry every {_reset_interval // 60} minute(s) "
                    f"for up to {(_reset_max * _reset_interval) // 60} minutes "
                    f"before the script exits."
                )
                if _session_log:
                    _session_log.log(
                        f"[{label}] {host} SSH not responding "
                        f"(connection reset); retrying up to {_reset_max} "
                        f"times at {_reset_interval}s intervals",
                        prefix="WARN",
                    )
                _reset_done = False
                for _r in range(1, _reset_max + 1):
                    print(
                        f"   ⏳ [{label}] waiting {_reset_interval}s before "
                        f"retry {_r}/{_reset_max}..."
                    )
                    _waited = 0
                    while _waited < _reset_interval:
                        if _shutdown_event.is_set():
                            raise last_exc
                        time.sleep(1)
                        _waited += 1
                    try:
                        client = paramiko.SSHClient()
                        client.set_missing_host_key_policy(
                            paramiko.AutoAddPolicy())
                        print(
                            f"   🔌 [{label}] retrying SSH to {host} as "
                            f"{username} (reset retry {_r}/{_reset_max})..."
                        )
                        if _session_log:
                            _session_log.log(
                                f"[{label}] SSH reset retry {_r}/{_reset_max} "
                                f"to {host} as {username}"
                            )
                        client.connect(
                            hostname=host, username=username,
                            password=password, timeout=45,
                            banner_timeout=60, auth_timeout=45,
                            disabled_algorithms={"pubkeys": ["ssh-dss"]},
                        )
                        configure_transport(client)
                        _register_bmc_client(host, client)
                        return client, username, password
                    except Exception as e2:
                        last_exc = e2
                        msg2 = str(e2).lower()
                        if (isinstance(e2, ConnectionResetError)
                                or "connection reset" in msg2
                                or "reset by peer" in msg2):
                            print(
                                f"   ⚠️  [{label}] {host} still not "
                                f"responding (connection reset)."
                            )
                            continue
                        # Different failure (auth, banner, etc.) — break
                        # out and let the outer logic handle/raise it.
                        print(
                            f"   ⚠️  [{label}] retry surfaced a different "
                            f"error: {e2}"
                        )
                        _reset_done = True
                        break
                if not _reset_done:
                    print(
                        f"   ❌ [{label}] {host} SSH did not recover after "
                        f"{(_reset_max * _reset_interval) // 60} minutes; "
                        "aborting."
                    )
                    if _session_log:
                        _session_log.log(
                            f"[{label}] {host} SSH never recovered after "
                            f"{_reset_max} reset retries; aborting",
                            prefix="ERROR",
                        )
                    raise last_exc
                # _reset_done True → fall through to normal handling
                # below using last_exc for the new (non-reset) failure.
                e = last_exc
                msg = str(e).lower()
            # ── BMC SSH banner not received ───────────────────────────
            # The BMC SSH daemon is slow to start (post-reboot, busy
            # serving console, etc.). Wait 60s and retry, up to 5
            # minutes total (5 retries), before falling through to the
            # normal failure path.
            if "banner" in msg:
                _bnr_max = 5
                _bnr_interval = 60  # seconds
                print(
                    f"   ⚠️  [{label}] BMC SSH banner not received from "
                    f"{host} (BMC may still be starting up). Waiting "
                    f"{_bnr_interval}s and retrying (up to "
                    f"{(_bnr_max * _bnr_interval) // 60} minutes total)..."
                )
                if _session_log:
                    _session_log.log(
                        f"[{label}] banner timeout for {host}; retrying "
                        f"up to {_bnr_max} times at {_bnr_interval}s "
                        "intervals",
                        prefix="WARN",
                    )
                _bnr_done = False
                for _b in range(1, _bnr_max + 1):
                    # Before each retry sleep, try to free up a BMC SSH
                    # session slot: close any in-process clients we still
                    # hold to this host, run 'ipmitool sol deactivate'
                    # (cheap, often unblocks a stuck SOL session), and
                    # — when --auto-clear-stale-bmc is set — SIGTERM
                    # prior-run python PIDs that still hold a TCP socket
                    # to <host>:22.
                    with suppress(Exception):
                        _clear_stale_bmc_sessions(
                            host, username, password, log=_session_log,
                        )
                    _waited = 0
                    while _waited < _bnr_interval:
                        if _shutdown_event.is_set():
                            raise last_exc
                        time.sleep(1)
                        _waited += 1
                    try:
                        client = paramiko.SSHClient()
                        client.set_missing_host_key_policy(
                            paramiko.AutoAddPolicy())
                        print(
                            f"   🔌 [{label}] retrying SSH to {host} as "
                            f"{username} (banner retry {_b}/{_bnr_max})..."
                        )
                        if _session_log:
                            _session_log.log(
                                f"[{label}] SSH banner retry {_b}/{_bnr_max} "
                                f"to {host} as {username}"
                            )
                        client.connect(
                            hostname=host, username=username,
                            password=password, timeout=45,
                            banner_timeout=60, auth_timeout=45,
                            disabled_algorithms={"pubkeys": ["ssh-dss"]},
                        )
                        configure_transport(client)
                        _register_bmc_client(host, client)
                        return client, username, password
                    except Exception as eb:
                        last_exc = eb
                        msg_b = str(eb).lower()
                        if "banner" in msg_b:
                            print(
                                f"   ⚠️  [{label}] {host} still not "
                                "responding to SSH (banner timeout)."
                            )
                            continue
                        # Different failure — let outer logic handle it.
                        print(
                            f"   ⚠️  [{label}] retry surfaced a different "
                            f"error: {eb}"
                        )
                        _bnr_done = True
                        break
                if not _bnr_done:
                    print(
                        f"   ❌ [{label}] {host} SSH banner never arrived "
                        f"after {(_bnr_max * _bnr_interval) // 60} minutes."
                    )
                    if _session_log:
                        _session_log.log(
                            f"[{label}] {host} SSH banner never arrived "
                            f"after {_bnr_max} retries; aborting",
                            prefix="ERROR",
                        )
                    raise last_exc
                # _bnr_done True → a non-banner failure surfaced; fall
                # through to normal handling with the new exception.
                e = last_exc
                msg = str(e).lower()
            # Map paramiko's noisy "Error reading SSH protocol banner" to a
            # clearer one-liner. The BMC is usually just slow to start its
            # SSH daemon (post-reboot, BMC busy serving console, etc.).
            if "banner" in msg:
                friendly = ("BMC SSH banner not received in time "
                            "(BMC may still be starting up)")
                print(f"   ⚠️  [{label}] connect attempt {attempt} failed: {friendly}")
                if _session_log:
                    _session_log.log(
                        f"[{label}] banner timeout for {host}: {e}",
                        prefix="WARN",
                    )
            else:
                print(f"   ⚠️  [{label}] connect attempt {attempt} failed: {e}")
                if _session_log:
                    _session_log.log(
                        f"[{label}] connect attempt {attempt} failed: {e}",
                        prefix="ERROR",
                    )
            # Treat "authentication"/"auth"/"password" hints as auth failures
            # too, since some paramiko/server combos surface them as generic
            # SSHException.
            if interactive and ("authentication" in msg or "auth" in msg
                                or "password" in msg) and attempt < max_attempts:
                # Exhaust silent fallbacks before prompting.
                if _queue_idx < len(_attempt_queue):
                    continue
                try:
                    with _stdin_lock:
                        print(f"\n   \U0001F510 Re-enter credentials for {host} "
                              f"(was: {username})")
                        new_user = input(
                            f"   Enter username for {host} [{username}] (blank to keep): "
                        ).strip() or username
                        new_pass = getpass.getpass(
                            f"   Enter password for {new_user}@{host}: "
                        )
                except (EOFError, KeyboardInterrupt):
                    break
                if not new_pass:
                    break
                username, password = new_user, new_pass
                continue
            time.sleep(min(5 * attempt, 15))
    raise last_exc if last_exc else RuntimeError(
        f"Could not connect to {host} after {max_attempts} attempts"
    )


def connect_to_sp(host, username, password):
    """Connect to the primary BMC. Returns (client, username, password) so
    the caller can update stored creds if the user re-entered them after an
    initial auth failure.
    """
    global _active_client, _primary_bmc_user, _primary_bmc_password
    print(f"Connecting to SP at {host} with username {username}...")
    _slog(f"Connecting to SP at {host} with username {username}")
    try:
        client, username, password = _ssh_connect_with_retry(
            host, username, password, label="primary BMC", max_attempts=5,
            interactive=True,
        )
    except Exception as e:
        print(f"❌ Error connecting to SP: {e}")
        _slog(f"SSH connection failed: {e}", prefix="ERROR")
        sys.exit(1)
    print("✅ Connection successful!")
    _slog("SSH connection established successfully")
    with _client_lock:
        _active_client = client
    _primary_bmc_user = username
    _primary_bmc_password = password
    return client, username, password


def is_session_alive(client, channel):
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            return False
        return not channel.closed
    except Exception:
        return False


def reconnect_to_sp(host, username, password):
    """Reconnect after a session drop. Retries with the same credentials,
    falling back to prompting if the BMC's password has changed.
    """
    global _active_client
    try:
        client, username, password = _ssh_connect_with_retry(
            host, username, password, label="reconnect", max_attempts=5,
            interactive=True,
        )
    except Exception as e:
        print(f"❌ Could not reconnect: {e}")
        _slog(f"All reconnection attempts failed: {e}", prefix="ERROR")
        return None, None
    channel = _open_shell(client)
    print("✅ Reconnected!")
    _slog("Reconnected successfully")
    with _client_lock:
        _active_client = client
    return client, channel


# ---------------------------------------------------------------------------
# Keepalive thread
# ---------------------------------------------------------------------------

def keepalive_loop(client):
    logger = logging.getLogger("keepalive")
    while not _shutdown_event.is_set():
        try:
            with _client_lock:
                c = _active_client or client
            transport = c.get_transport()
            if transport and transport.is_active():
                transport.send_ignore()
                logger.debug("Keepalive sent")
        except Exception as e:
            logger.debug("Keepalive error: %s", e)
        _shutdown_event.wait(5)


# ---------------------------------------------------------------------------
# Direct channel I/O
# ---------------------------------------------------------------------------

def _reclaim_system_console(channel, node_log=None):
    """Re-enter system console after an unexpected drop to the BMC prompt.
    Auto-answers 'y' to any existing-session takeover question.
    Called from within the read loops when _BMC_PROMPT_SIG is detected.
    """
    print("\n⚠️  BMC prompt detected – reconnecting to system console...")
    _slog("BMC prompt seen mid-wait; re-sending 'system console'", prefix="WARN")
    channel.send("system console\r")
    if _session_log:
        _session_log.log_sent("system console")
    time.sleep(0.5)
    buf = ""
    _rc_deadline = time.monotonic() + 15
    while time.monotonic() < _rc_deadline:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            buf += chunk
            if node_log:
                _par_write(node_log, chunk)
            if _session_log:
                _session_log.log_console(chunk)
            buf_lower = buf.lower()
            if "y/n" in buf_lower:
                print("⚠️  Existing console session – auto-disconnecting (y)...")
                _slog("Existing console session; auto-sending 'y'", prefix="WARN")
                channel.send("y\r")
                if _session_log:
                    _session_log.log_sent("y")
                time.sleep(1)
                print("✅ System console reconnected.")
                _slog("System console reconnected after auto-takeover")
                return True
            if any(s in buf_lower for s in
                   ("ctrl-d", "type exit", "serial", "loader", "autoboot", "selection")):
                print("✅ System console reconnected.")
                _slog("System console reconnected")
                return True
        time.sleep(0.1)
    print("⚠️  System console reconnect timed out – continuing anyway.")
    _slog("System console reconnect timed out", prefix="WARN")
    return False


def _recv_loop(channel, matchers, timeout=15, node_log=None, check_bmc_drop=False):
    """Shared receive loop used by direct_send_and_wait / direct_read_until /
    direct_read_until_any.

    ``matchers`` is a list of ``(lower_str, original_str)`` pairs.  The loop
    reads from *channel* until one of the matchers is found in the accumulated
    output or *timeout* seconds elapses.

    Returns ``(output, matched_original)`` where *matched_original* is the
    original (non-lower-cased) string from *matchers* that triggered the
    match, or ``None`` on timeout / shutdown.  The full accumulated *output*
    string is always returned unmodified so callers can inspect it.
    """
    output = ""
    output_lower = ""
    start_time = time.monotonic()
    while True:
        if _shutdown_event.is_set():
            return output, None
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            output_lower += chunk.lower()
            if len(output_lower) > 16384:
                output_lower = output_lower[-8192:]
            if node_log:
                _par_write(node_log, chunk)
            elif not _console_quiet:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            if check_bmc_drop and _BMC_PROMPT_SIG in chunk.lower():
                _rc_t = time.monotonic()
                _reclaim_system_console(channel, node_log=node_log)
                start_time += time.monotonic() - _rc_t
                output = ""
                output_lower = ""
                continue
            for lower_m, orig_m in matchers:
                if lower_m in output_lower:
                    return output, orig_m
        if time.monotonic() - start_time > timeout:
            return output, None
        time.sleep(0.1)


def direct_send_and_wait(channel, command, look_for, timeout=15, auto_respond=None,
                         node_log=None, check_bmc_drop=False, quiet=False):
    if _session_log and command:
        _session_log.log_sent(command)
    if command:
        channel.send(command + "\r")
    matchers = [(look_for.lower(), look_for)] if look_for else []
    output, matched = _recv_loop(channel, matchers, timeout, node_log, check_bmc_drop)
    if matched is None:
        _slog(f"Timeout ({timeout}s) waiting for '{look_for}'", prefix="WARN")
    elif auto_respond:
        time.sleep(0.3)
        channel.send(auto_respond + "\r")
        if not quiet:
            print(f"\n✅ Detected '{look_for}' – auto-responded with '{auto_respond}'")
        elif node_log:
            _par_write(node_log, f"\n>>> auto-responded to '{look_for}' with '{auto_respond}'\n")
        if _session_log:
            _session_log.log(f"Detected '{look_for}' – auto-responded with '{auto_respond}'")
            _session_log.log_sent(auto_respond)
    return output


def direct_read_until(channel, look_for, timeout=15, node_log=None, check_bmc_drop=False):
    matchers = [(look_for.lower(), look_for)] if look_for else []
    output, matched = _recv_loop(channel, matchers, timeout, node_log, check_bmc_drop)
    if matched is None and _session_log:
        _session_log.log(f"Timeout ({timeout}s) waiting for '{look_for}'", prefix="WARN")
    return output


def direct_read_until_any(channel, look_for_list, timeout=15, node_log=None, check_bmc_drop=False):
    matchers = [(s.lower(), s) for s in look_for_list]
    output, matched = _recv_loop(channel, matchers, timeout, node_log, check_bmc_drop)
    if matched is None and _session_log:
        _session_log.log(f"Timeout ({timeout}s) waiting for any of {look_for_list}", prefix="WARN")
    return output, matched


def drain_channel(channel, seconds=2, node_log=None):
    output = ""
    start_time = time.monotonic()
    while time.monotonic() - start_time < seconds:
        if _shutdown_event.is_set():
            return output
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            if node_log:
                _par_write(node_log, chunk)
            elif not _console_quiet:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
        time.sleep(0.1)
    return output


# ---------------------------------------------------------------------------
# Signal handler – force exit on second Ctrl+C
# ---------------------------------------------------------------------------

def signal_handler(sig, frame):
    global _ctrl_c_count
    _ctrl_c_count += 1

    if _ctrl_c_count == 1:
        print("\n👋 Received termination signal. Cleaning up...")
        _slog("Received termination signal (Ctrl+C or SIGTERM)")
        _shutdown_event.set()
    else:
        print("\n⚡ Force exit!")
        _restore_terminal()
        if _session_log:
            try:
                _session_log.log("Force exit (second Ctrl+C)")
                _session_log.set_outcome("FAIL", "force exit (second Ctrl+C / signal)")
                _session_log.close()
            except Exception:
                pass
        os._exit(1)


def _print_man_page():
    """Print a man page-style reference for all CLI options."""
    # Fit to terminal width, capped at 100 columns.
    cols = min(shutil.get_terminal_size((80, 24)).columns, 100)
    rule = "─" * cols

    page = f"""
{rule}
NAME
    AFX_reinit.py — NetApp AFX cluster reinitialization automation script

SYNOPSIS
    python3 AFX_reinit.py [OPTIONS]

DESCRIPTION
    Automated BMC/SP console management tool for reinitializing NetApp AFX
    cluster nodes.  Connects to each node's Baseboard Management Controller
    (BMC) or Service Processor (SP) via SSH, drives the LOADER boot sequence,
    and automates the ONTAP cluster-setup and node-join wizards.

    Operation modes (selected interactively at startup):

      1a   Initialize first node — interactive wizard
      1b   Initialize first node — fully automated
      2a   Add node to existing cluster — interactive wizard
      2b   Add node to existing cluster — automated (parallel multi-node)
       3   End-to-end reinit: mode 1b on primary + mode 2b on all peers
      4a   ONTAP rolling upgrade (takeover / software update / giveback)
      4b   Netboot and install ONTAP image
      4c   Standalone license install
      4d   Set up passwordless SSH to cluster management
      4e   Create / save cluster configuration backup (JSON)
      4f   Verify BMC SSH authentication for all nodes

OPTIONS
    -h, --help
        Print this help page and exit.

    -c PATH, --config PATH
        Path to a JSON configuration file containing cluster and node
        parameters (BMC addresses, credentials, management IPs, etc.).
        If omitted, the script searches standard locations automatically
        and prompts for any missing values at runtime.

        Use --config-example to print an annotated template.

    --config-example
        Print an annotated example configuration file to stdout and exit.
        Redirect to a file to create a starting template:

            python3 AFX_reinit.py --config-example > reinit-config.json

    -d, --debug
        Debug mode.  Prints all raw BMC/SP and ONTAP console I/O directly
        to the terminal (in addition to the log file).  Also sets Python
        logging to DEBUG level, exposing verbose Paramiko SSH messages.
        Use this to diagnose hangs, prompt-matching failures, or SSH
        authentication issues.

    --bg
        Background mode.  Registers a SIGHUP handler so the session log is
        flushed and closed cleanly if the controlling terminal disconnects.
        Use with nohup(1) or inside a screen/tmux session.

        Note: SIGHUP is not supported on Windows.  The flag is accepted but
        has no effect on that platform.

    --screen
        Re-launch the script inside a detached GNU screen(1) session, then
        exit the outer process.  The run continues unattended in the
        background; if your SSH connection drops or times out, reattach from
        any new terminal.

        Behaviour:
          • Checks the STY environment variable.  If STY is set (already
            inside a screen session) this flag is silently ignored and the
            script proceeds normally — no recursive spawn.
          • Exits with install instructions if screen(1) is not found.
          • Automatically appends --bg to the forwarded argument list.
          • Detached session name: afx-reinit

        Reattach with:
            screen -r afx-reinit

        List all sessions:
            screen -ls

        Note: GNU screen is available on Linux and macOS only.  On Windows
        use WSL or a Linux jump host for equivalent functionality.

    --resume
        Mode 4b only.  Resume the previous 4b run from its saved
        checkpoint (afx_checkpoint.json, located alongside this script).
        Phases already completed are skipped — when every BMC IP is
        marked install_done the run jumps straight to Step 6b
        (reconnect to LOADER + boot_ontap menu); peers marked
        peer_joined are skipped during the mode-3 parallel auto-add.

        If primary_setup_done or option3_complete is set from a prior
        run, the resume prompt warns that re-running will destroy the
        existing cluster and asks for explicit confirmation.

        Checkpoints older than 72 hours are ignored.  The file is
        deleted automatically on successful completion of mode 4b.

    --checkpoint-status
        Print a summary of the saved checkpoint (afx_checkpoint.json)
        and exit.  Shows the absolute file path, run mode, created /
        updated timestamps, age in minutes, log directory, config
        path, BMC IPs, every completed global phase, and every per-node
        phase keyed by BMC IP.  Does not modify the checkpoint file.

EXAMPLES
    Interactive run (prompts for all values):
        python3 AFX_reinit.py

    Automated run with config file:
        python3 AFX_reinit.py --config configs/reinit-config.json

    Print config file template:
        python3 AFX_reinit.py --config-example > configs/reinit-config.json

    Debug a failed run:
        python3 AFX_reinit.py --debug --config configs/reinit-config.json

    Protected remote run (auto screen session):
        python3 AFX_reinit.py --screen --config configs/reinit-config.json

    Reattach to a running screen session:
        screen -r afx-reinit

    Manual nohup background run:
        nohup python3 AFX_reinit.py --bg --config configs/reinit-config.json \\
              > nohup.out 2>&1 &

    Inspect a saved 4b checkpoint:
        python3 AFX_reinit.py --checkpoint-status

    Resume an interrupted 4b run:
        python3 AFX_reinit.py --resume --config configs/reinit-config.json

FILES
    reinit-config.json
        Default configuration file name.  Searched in ./configs/, the script
        directory, then the current working directory.

    logs/YYYYMMDD_HHMMSS/session_<label>.log
        Full raw console transcript for the session.

    logs/YYYYMMDD_HHMMSS/summary_<label>.log
        Human-readable summary: result (PASS/FAIL/WARN), phase and step
        timing, warnings inventory, and errors inventory.

    afx_checkpoint.json
        Mode 4b resume checkpoint.  Written alongside this script; deleted
        automatically on successful completion.  Inspect with
        --checkpoint-status; resume with --resume.  Ignored if older than
        72 hours.

ENVIRONMENT
    STY     Set by GNU screen for all child processes.  When present,
            --screen is a no-op (script is already inside a screen session).

EXIT STATUS
    0   Success (or --help / --config-example printed and exited cleanly).
    1   Fatal error (connection failure, timeout, unhandled exception, or
        screen(1) not found when --screen was requested).

SEE ALSO
    screen(1), nohup(1), ssh(1), python3(1)
    NetApp ONTAP documentation: https://docs.netapp.com/us-en/ontap/
{rule}"""
    print(page)


def parse_args():
    # Disable argparse's built-in -h/--help so we can provide a man-page
    # style replacement instead.
    parser = argparse.ArgumentParser(
        description="NetApp AFX BMC console automation script 🤖",
        add_help=False,
    )
    parser.add_argument("--help", "-h", action="store_true", default=False,
                        help="Print this help page and exit.")
    parser.add_argument("--debug", "-d", action="store_true", default=False,
                        help="Debug mode: print all raw console output to the "
                             "screen instead of suppressing it to the log file. "
                             "Also enables verbose Python logging.")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to a JSON config file with cluster/node "
                             "settings. Use --config-example to print the "
                             "expected format.")
    parser.add_argument("--config-example", action="store_true", default=False,
                        help="Print an example config file and exit.")
    parser.add_argument("--bg", action="store_true", default=False,
                        help="Run in background / non-interactive mode. "
                             "Handles SIGHUP so the log is closed cleanly "
                             "when the terminal closes.")
    parser.add_argument("--screen", action="store_true", default=False,
                        help="Re-launch the script inside a GNU screen session "
                             "so it keeps running if your SSH connection drops "
                             "or times out. Implies --bg. Use "
                             "'screen -r afx-reinit' to reattach. "
                             "No-op if already running inside screen.")
    parser.add_argument("--resume", action="store_true", default=False,
                        help="Resume a previous 4b run from its last saved "
                             "checkpoint (afx_checkpoint.json). Skips phases "
                             "already completed so you do not have to restart "
                             "from scratch after a failure.")
    parser.add_argument("--checkpoint-status", action="store_true", default=False,
                        help="Print a summary of the saved checkpoint "
                             "(afx_checkpoint.json) showing exactly where the "
                             "last run left off, then exit. Does not modify "
                             "the checkpoint file.")
    parser.add_argument("--auto-clear-stale-bmc", action="store_true",
                        default=False,
                        help="When a BMC SSH banner timeout is hit, scan for "
                             "ESTABLISHED TCP sockets to <bmc>:22 from OTHER "
                             "python processes on this host and SIGTERM them "
                             "(prior AFX_reinit runs that died without "
                             "closing their SSH sessions are the usual "
                             "culprits). Always-on cleanup (closing this "
                             "process's own SSH clients + ipmitool sol "
                             "deactivate) runs regardless of this flag.")
    args = parser.parse_args()
    if args.help:
        _print_man_page()
        sys.exit(0)
    return args


# ---------------------------------------------------------------------------
# Screen session launcher
# ---------------------------------------------------------------------------

def _relaunch_in_screen():
    """Re-exec this script inside a detached GNU screen session.

    Call this when ``--screen`` is requested.  Returns ``True`` if the
    relaunch was performed (the caller should then ``sys.exit(0)``).  Returns
    ``False`` if we are already inside a screen session, so the caller should
    continue running normally.
    """
    # Already inside a screen session — STY is set by screen for every child.
    if os.environ.get("STY"):
        print("ℹ️   Already running inside a screen session "
              f"(STY={os.environ['STY']}). Continuing normally.")
        return False

    screen_bin = shutil.which("screen")
    if not screen_bin:
        print("❌  'screen' is not installed or not found in PATH.")
        print("    Install it first:")
        print("      Ubuntu/Debian : sudo apt install screen")
        print("      RHEL/Fedora   : sudo dnf install screen")
        print("    Then re-run with --screen, or run the script directly "
              "without --screen.")
        sys.exit(1)

    session_name = "afx-reinit"

    # Forward all original argv except '--screen' to avoid infinite recursion.
    # Always include '--bg' so the log is flushed cleanly on detach/SIGHUP.
    fwd_args = [a for a in sys.argv[1:] if a != "--screen"]
    if "--bg" not in fwd_args:
        fwd_args.append("--bg")

    script_path = os.path.abspath(sys.argv[0])
    cmd = [screen_bin, "-dmS", session_name,
           sys.executable, script_path] + fwd_args

    print(f"🖥️   Launching inside screen session '{session_name}'...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"❌  screen exited with code {exc.returncode}.")
        sys.exit(exc.returncode)

    print("\n✅  Script is running in the background.")
    print(f"    Reattach with : screen -r {session_name}")
    print("    List sessions : screen -ls")
    return True


# ---------------------------------------------------------------------------
# Wait for BMC prompt
# ---------------------------------------------------------------------------

def wait_for_bmc_prompt(channel, auto_takeover=False):
    print("Shell invoked. Waiting for initial prompt...")
    _slog("Waiting for initial BMC prompt (watching for existing session y/n)")

    output, matched = direct_read_until_any(channel, ["y/n", ">"], timeout=15)

    if matched and "y/n" in matched.lower():
        print("\n⚠️  An existing session is active on this BMC!")
        if auto_takeover:
            print("   Auto-disconnecting existing session...")
            if _session_log:
                _session_log.log("Auto-takeover: disconnecting existing BMC session", prefix="WARN")
                _session_log.log_sent("y")
            answer = "y"
        else:
            answer = input("   Do you want to disconnect the other session? [Y/N]: ").strip().lower()
            if _session_log:
                _session_log.log_user_input(f"Existing session takeover response: {answer}")

        if answer == "y":
            print("Disconnecting other session...")
            if _session_log and not auto_takeover:
                _session_log.log("User chose to take over existing session")
                _session_log.log_sent("y")
            channel.send("y\r")
            time.sleep(2)

            output = direct_read_until(channel, ">", timeout=15)
            if ">" not in output:
                print("❌ Did not receive BMC prompt after session takeover. Exiting.")
                _slog("BMC prompt not received after session takeover", prefix="ERROR")
                return False
            print("✅ BMC prompt detected after session takeover.")
            _slog("BMC prompt detected after session takeover")
            return True
        else:
            print("❌ Cannot continue without taking over the session. Exiting.")
            if _session_log:
                _session_log.log("User declined to take over existing session – exiting")
                _session_log.log_sent("n")
            channel.send("n\r")
            return False

    elif matched and ">" in matched:
        print("✅ BMC prompt detected.")
        _slog("BMC prompt detected (no existing session)")
        return True

    else:
        print("❌ Did not receive BMC prompt. Exiting.")
        _slog("BMC prompt not received – timeout", prefix="ERROR")
        return False


# ---------------------------------------------------------------------------
# Enter system console
# ---------------------------------------------------------------------------

def enter_system_console(channel):
    print("\n📺 Probing current prompt before entering system console...")
    _slog("Probing prompt before system console")

    # Hit Enter to get the current prompt.
    channel.send("\r")
    probe_out, probe_matched = direct_read_until_any(
        channel,
        ["::>", "bmc", "login:", ">", "#"],
        timeout=10,
    )
    probe_lower = probe_out.lower()

    if "::>" in probe_out:
        # Already in the cluster shell – nothing to do.
        print("✅ Already in cluster shell (::> detected). Skipping 'system console'.")
        _slog("Already in cluster shell – system console skipped")
        return

    if "login:" in probe_lower:
        # At a cluster login prompt – also already past the BMC.
        print("✅ Cluster login prompt detected. Skipping 'system console'.")
        _slog("Cluster login prompt detected – system console skipped")
        return

    # BMC prompt (or unrecognised) – proceed with "system console".
    if "bmc" not in probe_lower:
        print("⚠️  Prompt unrecognised; attempting 'system console' anyway...")
        if _session_log:
            _session_log.log(f"Prompt unrecognised, sending system console anyway. Probe: {probe_out[-200:]!r}",
                             prefix="WARN")
    else:
        print("✅ BMC prompt detected. Entering system console...")
        _slog("BMC prompt confirmed; sending 'system console'")

    if _session_log:
        _session_log.log_sent("system console")

    channel.send("system console\r")

    print("Waiting for system console response...")
    output, matched = direct_read_until_any(
        channel,
        ["y/n", "ctrl-d", "type exit", "serial console", "boot loader", "loader", "autoboot"],
        timeout=15
    )

    if matched and "y/n" in matched.lower():
        print("\n⚠️  An existing console session is active!")
        answer = input("   Do you want to disconnect the other console session? [Y/N]: ").strip().lower()
        if _session_log:
            _session_log.log_user_input(f"Existing console session takeover response: {answer}")

        if answer == "y":
            print("Disconnecting other console session...")
            if _session_log:
                _session_log.log("User chose to take over existing console session")
                _session_log.log_sent("y")
            channel.send("y\r")
            time.sleep(2)

            print("Waiting for console to connect after takeover...")
            output2, matched2 = direct_read_until_any(
                channel,
                ["ctrl-d", "type exit", "serial console", "boot loader", "loader", "autoboot", ">"],
                timeout=15
            )
            if matched2:
                print("✅ System console connected after session takeover.")
                _slog("System console connected after session takeover")
            else:
                print("⚠️  No console confirmation after takeover, continuing anyway...")
                _slog("No console confirmation after takeover", prefix="WARN")
        else:
            print("❌ Cannot continue without console access. Exiting.")
            if _session_log:
                _session_log.log("User declined to take over console session – exiting")
                _session_log.log_sent("n")
            channel.send("n\r")
            sys.exit(1)

    elif matched:
        print("✅ System console connected.")
        _slog(f"System console connected (matched: {matched})")
    else:
        print("⚠️  No console confirmation detected, continuing anyway...")
        _slog("No console confirmation detected", prefix="WARN")

    drain_channel(channel, seconds=3)
    print("✅ System console ready.\n")
    print("⏳ LOADER will appear. Script is continuing. Be patient.\n")
    _slog("System console ready")


# ---------------------------------------------------------------------------
# Retain-config helpers (mode 1: capture cluster name / network LIFs)
# ---------------------------------------------------------------------------

def _build_credential_list(*triplets):
    """Return an ordered, deduplicated list of (user, password, source) tuples.

    Each element of ``triplets`` must itself be a (user, password, source)
    tuple. Entries with None/blank user or password are silently dropped.
    Duplicate (user, password) pairs keep only their first occurrence.
    """
    seen = set()
    result = []
    for user, password, source in triplets:
        if isinstance(user, str):
            user = user.strip() or None
        if isinstance(password, str):
            password = password.strip() or None
        if not user or not password:
            continue
        key = (user, password)
        if key in seen:
            continue
        seen.add(key)
        result.append((user, password, source))
    return result


def _candidate_cluster_logins():
    """Return an ordered list of (user, password, source) candidates to try
    against the cluster console `login:` prompt. Drops duplicates and any
    incomplete pairs.
    """
    cfg_cluster = _config_data.get("cluster") or {}
    return _build_credential_list(
        (_cluster_config.get("admin_user"),
         _cluster_config.get("admin_password"),
         "previous cluster login"),
        (cfg_cluster.get("user") or "admin",
         cfg_cluster.get("password"),
         "config file"),
        (_primary_bmc_user, _primary_bmc_password, "BMC credentials"),
    )


def _attempt_console_cluster_login(channel):
    """Authenticate at a cluster console `login:` prompt by trying known
    credentials silently (config file / cached cluster login / BMC creds)
    before falling back to interactive prompts. Returns True on success.

    The caller is expected to have just observed `login:` in the output;
    this function then sends the username, waits for `password:`, sends the
    password, and verifies a cluster prompt (`::>` / `::*>`) appears.
    """
    print("\n   \U0001F510 Cluster login required.")
    candidates = _candidate_cluster_logins()

    def _try_pair(user, password, source, log_attempt=True):
        if log_attempt and _session_log:
            _session_log.log(f"Cluster login attempt via {source} (user={user})")
        # Send username.
        if _session_log:
            _session_log.log_sent(user)
        channel.send(user + "\r")
        out, m = direct_read_until_any(
            channel, ["password:", "::>", "::*>", "login:"], timeout=15,
        )
        if m and ("::>" in m or "::*>" in m):
            return True
        if not (m and "password:" in m.lower()):
            return False
        # Send password (never echoed/logged in clear text).
        _slog("Cluster password sent (<hidden>)")
        channel.send(password + "\r")
        out, m = direct_read_until_any(
            channel, ["::>", "::*>", "login:"], timeout=20,
        )
        return bool(m and ("::>" in m or "::*>" in m))

    # 1) Silent attempts using known creds.
    for user, password, source in candidates:
        print(f"   \U0001F511 Trying cluster login via {source} (user={user})...")
        if _try_pair(user, password, source):
            print(f"   \u2705 Cluster login succeeded via {source}.")
            _cluster_config["admin_user"] = user
            _cluster_config["admin_password"] = password
            _slog(f"Cluster login succeeded via {source}")
            return True
        print(f"   \u26A0\uFE0F  Login via {source} failed; trying next option.")
        _slog(f"Cluster login via {source} failed", prefix="WARN")
        # Drain any residual output before the next attempt.
        drain_channel(channel, seconds=0.5)

    # 2) Interactive fallback – re-prompt until success or the operator
    # gives up (Ctrl+C / EOF).
    default_user = (candidates[-1][0] if candidates else "admin")
    while True:
        try:
            entered_user = input(
                f"   Cluster admin username [{default_user}]: "
            ).strip() or default_user
            entered_pass = getpass.getpass("   Cluster admin password: ")
        except (EOFError, KeyboardInterrupt):
            print("   \u274C Cluster login aborted by operator.")
            _slog("Cluster login aborted by operator", prefix="ERROR")
            return False
        if not entered_pass:
            print("   \u26A0\uFE0F  Password cannot be empty.")
            continue
        if _session_log:
            _session_log.log_user_input(f"Cluster login user (manual): {entered_user}")
        if _try_pair(entered_user, entered_pass, "manual entry", log_attempt=False):
            print("   \u2705 Cluster login succeeded.")
            _cluster_config["admin_user"] = entered_user
            _cluster_config["admin_password"] = entered_pass
            _slog("Cluster login succeeded via manual entry")
            return True
        print("   \u274C Login failed; please re-enter.")
        _slog("Manual cluster login failed; re-prompting", prefix="WARN")
        drain_channel(channel, seconds=0.5)


def _wait_for_cluster_prompt(channel, timeout=30):
    """Wake the console and wait for the ONTAP cluster shell prompt.

    If a login prompt appears, collect credentials interactively. Returns
    True when at the cluster prompt, False otherwise.
    """
    channel.send("\r")
    output, matched = direct_read_until_any(
        channel, ["login:", "::>", "::*>"], timeout=timeout
    )

    if matched and "login:" in matched.lower():
        if not _attempt_console_cluster_login(channel):
            return False
        # `_attempt_console_cluster_login` already verified that `::>`/`::*>`
        # appeared in response to a successful auth, so we're at the cluster
        # shell prompt now.
        return True

    return bool(matched and ("::>" in matched or "::*>" in matched))


def _run_cluster_command(channel, cmd, timeout=60):
    """Send a cluster-shell command, return output captured up to next prompt.

    Uses the cluster prompt regex anchored to the tail of the buffer so that
    the echoed command/prompt fragments mid-output don't falsely terminate.
    """
    drain_channel(channel, seconds=0.3)
    if _session_log:
        _session_log.log_sent(cmd)
    channel.send(cmd + "\r")

    output = ""
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        if _shutdown_event.is_set():
            break
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            if not _console_quiet:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            if _CLUSTER_PROMPT_RE.search(output[-200:]):
                # Brief drain in case more output is still arriving.
                time.sleep(0.3)
                while channel.recv_ready():
                    extra = channel.recv(4096).decode("utf-8", errors="replace")
                    output += extra
                    if not _console_quiet:
                        sys.stdout.write(extra)
                        sys.stdout.flush()
                    if _session_log:
                        _session_log.log_console(extra)
                return output
        time.sleep(0.1)

    _slog(f"Timeout ({timeout}s) waiting for cluster prompt after '{cmd}'", prefix="WARN")
    return output


def _parse_keyed_table(output, command_prefix):
    """Parse a fields-style ONTAP table into a single dict keyed by header.

    Suitable for commands that return a single row of metadata such as
    ``cluster identity show -fields name,contact,location`` or
    ``vserver services name-service dns show -fields domains,name-servers``.
    Returns {} on parse failure or empty output.
    """
    headers = None
    dashes_seen = False
    for line in output.splitlines():
        s = line.strip()
        if not s:
            continue
        if "::" in s or s.lower().startswith(command_prefix.lower()):
            continue
        if "entries were displayed" in s.lower():
            continue
        if set(s) <= {"-", " "}:
            dashes_seen = True
            continue
        tokens = s.split()
        if not dashes_seen:
            lowered = [t.lower() for t in tokens]
            # Heuristic: treat the first non-prompt line as a header row when
            # it doesn't look like data (no dotted-quad addresses, etc.).
            if any(c in tokens[0].lower() for c in ("name", "domain", "contact",
                                                     "location", "server",
                                                     "vserver")):
                headers = lowered
            continue
        if headers and tokens:
            # Pad the row out so zip pairs cleanly even if a trailing column
            # is blank (ONTAP renders empty fields as "-").
            row_tokens = tokens[: len(headers)]
            while len(row_tokens) < len(headers):
                row_tokens.append("-")
            return dict(zip(headers, row_tokens))
    return {}


def _parse_cluster_identity(output):
    """Return {'name', 'contact', 'location'} from 'cluster identity show'.
    Missing fields are None.
    """
    row = _parse_keyed_table(output, "cluster identity")
    def _clean(v):
        if not v or v in ("-", "--"):
            return None
        return v
    return {
        "name": _clean(row.get("name")),
        "contact": _clean(row.get("contact")),
        "location": _clean(row.get("location")),
    }


def _parse_dns_config(output):
    """Return {'domains', 'name-servers'} from
    'vserver services name-service dns show'. Each value is the raw
    comma-separated string (or None).
    """
    row = _parse_keyed_table(output, "vserver services name-service dns")
    def _clean(v):
        if not v or v in ("-", "--"):
            return None
        return v
    # ONTAP emits the field names with hyphens.
    return {
        "domains": _clean(row.get("domains") or row.get("dns-domains")),
        "name-servers": _clean(row.get("name-servers") or row.get("servers")),
    }


def _parse_network_interfaces(output):
    """Parse 'net int show ... -fields ...' table output into list[dict]."""
    headers = None
    dashes_seen = False
    rows = []
    for line in output.splitlines():
        s = line.rstrip()
        stripped = s.strip()
        if not stripped:
            continue
        if "::" in stripped or stripped.startswith("(") or stripped.lower().startswith("net int"):
            continue
        if "entries were displayed" in stripped.lower():
            continue
        if set(stripped) <= {"-", " "}:
            dashes_seen = True
            continue
        tokens = stripped.split()
        if not dashes_seen:
            lowered = [t.lower() for t in tokens]
            if "address" in lowered and ("home-port" in lowered or "port" in lowered):
                headers = lowered
            continue
        if headers and len(tokens) == len(headers):
            rows.append(dict(zip(headers, tokens)))
    return rows


def _parse_sp_addresses(output):
    """Parse 'service-processor show -fields address[,node]' output.

    Populates the module-global ``_retained_sp_to_node`` map when both
    ``address`` and ``node`` columns are present, so per-node mgmt LIFs
    can later be promoted into the per-BMC config. Returns a list of
    SP/BMC IP strings (preserving discovery order).
    """
    headers = None
    dashes_seen = False
    addresses = []
    for line in output.splitlines():
        s = line.strip()
        if not s:
            continue
        if "::" in s or s.lower().startswith("service-processor"):
            continue
        if "entries were displayed" in s.lower():
            continue
        if set(s) <= {"-", " "}:
            dashes_seen = True
            continue
        tokens = s.split()
        if not dashes_seen:
            lowered = [t.lower() for t in tokens]
            if "address" in lowered:
                headers = lowered
            continue
        if headers and len(tokens) >= len(headers):
            row = dict(zip(headers, tokens[: len(headers)]))
            addr = row.get("address")
            node = row.get("node")
            if addr and addr not in ("-", "--"):
                addresses.append(addr)
                if node and node not in ("-", "--"):
                    _retained_sp_to_node[addr] = node
    return addresses


def _parse_matching_gateway(output, clus_mgmt_ip=None):
    """Parse 'route show -vserver <vserver> -fields gateway' output.

    Returns the gateway whose subnet contains *clus_mgmt_ip* when that IP is
    supplied.  Falls back to the first non-zero gateway in the output when
    there is no subnet match or when *clus_mgmt_ip* is None/blank.
    Returns None if no usable gateway is found.
    """
    _ipa = ipaddress  # local alias preserves existing references below

    # Resolve the cluster-mgmt IP object once so we can test containment.
    _mgmt_addr = None
    if clus_mgmt_ip:
        try:
            _mgmt_addr = _ipa.ip_address(clus_mgmt_ip.strip())
        except ValueError:
            pass

    # Walk the table looking for "destination" and "gateway" columns.
    headers = None
    dashes_seen = False
    first_gw = None  # first usable gateway (fallback)
    for line in output.splitlines():
        s = line.strip()
        if not s:
            continue
        if "::" in s or s.lower().startswith("route show"):
            continue
        if "entries were displayed" in s.lower():
            continue
        if set(s) <= {"-", " "}:
            dashes_seen = True
            continue
        tokens = s.split()
        if not dashes_seen:
            lowered = [t.lower() for t in tokens]
            if "gateway" in lowered:
                headers = lowered
            continue
        if not headers or len(tokens) < len(headers):
            continue
        row = dict(zip(headers, tokens[: len(headers)]))
        gw = (row.get("gateway") or "").strip()
        if not gw or gw in ("-", "--", "0.0.0.0"):
            continue
        try:
            _ipa.ip_address(gw)  # validate it is an IP
        except ValueError:
            continue
        if first_gw is None:
            first_gw = gw
        # If we have a destination column, try to match subnet.
        dest = (row.get("destination") or "").strip()
        if dest and _mgmt_addr is not None:
            try:
                net = _ipa.ip_network(dest, strict=False)
                if _mgmt_addr in net:
                    return gw  # best match
            except ValueError:
                pass

    # No subnet match found — return the first usable gateway.
    return first_gw


def _print_retain_summary(cluster_name, net_rows, peer_addresses=None):
    _print_banner("📝 Retained Configuration Summary")
    if cluster_name:
        print(f"\n  Cluster name: {cluster_name}")
    if net_rows:
        print("\n  Network interfaces:")
        print("  " + "-" * 88)
        print(f"  {'vserver':<14} {'lif':<22} {'home-node':<12} {'port':<8} "
              f"{'address':<16} {'netmask':<16} {'ipspace':<10}")
        print("  " + "-" * 88)
        for r in net_rows:
            print(
                f"  {r.get('vserver', '-'):<14} "
                f"{r.get('lif', '-'):<22} "
                f"{r.get('home-node', '-'):<12} "
                f"{r.get('home-port', r.get('port', '-')):<8} "
                f"{r.get('address', '-'):<16} "
                f"{r.get('netmask', '-'):<16} "
                f"{r.get('ipspace', '-'):<10}"
            )
    if peer_addresses:
        print("\n  Discovered service-processor (BMC) addresses:")
        for a in peer_addresses:
            print(f"    • {a}")
    if not cluster_name and not net_rows and not peer_addresses:
        print("\n  (Nothing was retained.)")
    print("")


def collect_retain_data(channel, retain_name, retain_network, collect_peer_sps=False):
    """Enter system console, capture requested data, return to BMC prompt.

    Each capture (cluster name, network LIFs, peer SP addresses) is attempted
    independently. If the cluster shell prompt cannot be reached (e.g. the
    node is already down), all captures are skipped gracefully and the caller
    proceeds with empty results.

    Returns (cluster_name, net_rows, peer_addresses). Any may be None / empty
    on failure.
    """
    global _retained_cluster_name, _retained_net_config, _retained_default_gateway

    if not (retain_name or retain_network or collect_peer_sps):
        return None, None, []

    purposes = []
    if retain_name or retain_network:
        purposes.append("retain config")
    if collect_peer_sps:
        purposes.append("peer BMC addresses")
    print(f"\n🔍 Entering system console to capture: {', '.join(purposes)}...")
    if _session_log:
        _session_log.start_phase("Capture Cluster Inventory")
        _session_log.log(
            f"Capturing cluster info (name={retain_name}, network={retain_network}, "
            f"peer_sps={collect_peer_sps})"
        )

    enter_system_console(channel)

    cluster_name = None
    net_rows = None
    peer_addresses = []

    if not _wait_for_cluster_prompt(channel, timeout=30):
        print("   ⚠️  Could not reach cluster shell prompt (node may be down).")
        print("      Skipping all captures and continuing without retained data")
        print("      or peer BMC addresses.")
        if _session_log:
            _session_log.log(
                "Could not reach cluster shell prompt; skipping all captures "
                "(retain + peer SP discovery)",
                prefix="WARN",
            )
    else:
        # Disable paging so multi-row output isn't truncated / paged.
        try:
            _run_cluster_command(channel, "rows 0", timeout=15)
        except Exception as e:
            _slog(f"'rows 0' failed: {e}", prefix="WARN")

        if retain_name:
            print("\n   ▶ cluster identity show -fields name,contact,location")
            try:
                out = _run_cluster_command(
                    channel,
                    "cluster identity show -fields name,contact,location",
                    timeout=30,
                )
                identity = _parse_cluster_identity(out)
                cluster_name = identity.get("name")
                global _retained_cluster_contact, _retained_cluster_location
                _retained_cluster_contact = identity.get("contact")
                _retained_cluster_location = identity.get("location")
            except Exception as e:
                _slog(f"cluster identity show failed: {e}", prefix="WARN")
            if _session_log:
                _session_log.log(
                    f"Captured cluster identity: name={cluster_name!r}, "
                    f"contact={_retained_cluster_contact!r}, "
                    f"location={_retained_cluster_location!r}"
                )

            # DNS configuration. Pulled alongside cluster identity so it's
            # available for the cluster-setup wizard (DNS domain + servers
            # prompts) when retaining the existing config.
            print("\n   ▶ vserver services name-service dns show")
            try:
                out = _run_cluster_command(
                    channel,
                    "vserver services name-service dns show "
                    "-fields domains,name-servers",
                    timeout=30,
                )
                dns = _parse_dns_config(out)
                global _retained_dns_domains, _retained_dns_servers
                _retained_dns_domains = dns.get("domains")
                _retained_dns_servers = dns.get("name-servers")
            except Exception as e:
                _slog(f"dns show failed: {e}", prefix="WARN")
            if _session_log:
                _session_log.log(
                    f"Captured DNS: domains={_retained_dns_domains!r}, "
                    f"servers={_retained_dns_servers!r}"
                )

        if retain_network:
            print("\n   ▶ net int show -role node-mgmt,cluster-mgmt,cluster -fields ...")
            try:
                out = _run_cluster_command(
                    channel,
                    "net int show -role node-mgmt,cluster-mgmt,cluster "
                    "-fields home-port,home-node,address,netmask,ipspace,role",
                    timeout=60,
                )
                net_rows = _parse_network_interfaces(out)
            except Exception as e:
                _slog(f"net int show failed: {e}", prefix="WARN")
            _slog(f"Captured {len(net_rows or [])} network interface rows")

            # Capture the cluster-management gateway using
            # 'route show -vserver <cluster_name> -fields gateway' and
            # select the gateway whose subnet contains the cluster-mgmt IP.
            # This is more precise than the generic default-route lookup.
            _gw_vserver = cluster_name or ""
            if _gw_vserver:
                _gw_cmd = (
                    f"route show -vserver {_gw_vserver} "
                    "-fields destination,gateway"
                )
            else:
                _gw_cmd = (
                    "network route show "
                    "-fields destination,gateway,vserver"
                )
            print(f"\n   ▶ {_gw_cmd}")
            # Determine the cluster-mgmt IP to use for subnet matching.
            _clus_mgmt_ip_for_gw = None
            if net_rows:
                for _r in net_rows:
                    if ((_r.get("role") or "").lower() == "cluster-mgmt"
                            or "cluster-mgmt" in (_r.get("ipspace") or "").lower()):
                        _clus_mgmt_ip_for_gw = _r.get("address")
                        break
            try:
                out = _run_cluster_command(channel, _gw_cmd, timeout=30)
                gw = _parse_matching_gateway(out, _clus_mgmt_ip_for_gw)
                if gw:
                    _retained_default_gateway = gw
                    if _session_log:
                        _session_log.log(
                            f"Captured cluster-mgmt gateway: {gw} "
                            f"(matched to mgmt IP {_clus_mgmt_ip_for_gw!r})"
                        )
                else:
                    if _session_log:
                        _session_log.log(
                            "No gateway parsed from route output", prefix="WARN"
                        )
            except Exception as e:
                _slog(f"route show for gateway failed: {e}", prefix="WARN")

        # Peer SP discovery is attempted independently of the retain captures
        # above (success or failure). It always runs when collect_peer_sps is
        # set, even if the user answered 'n' to both retain prompts.
        if collect_peer_sps:
            print("\n   ▶ service-processor show -fields address,node")
            try:
                out = _run_cluster_command(
                    channel,
                    "service-processor show -fields address,node",
                    timeout=30,
                )
                peer_addresses = _parse_sp_addresses(out)
            except Exception as e:
                if _session_log:
                    _session_log.log(
                        f"service-processor show failed: {e}", prefix="WARN"
                    )
            if _session_log:
                _session_log.log(
                    f"Captured {len(peer_addresses)} service-processor address(es): "
                    f"{peer_addresses}"
                )
                if _retained_sp_to_node:
                    _session_log.log(
                        f"SP -> node mapping: {_retained_sp_to_node}"
                    )

    # Exit system console back to BMC.
    print("\n   ↩️  Exiting system console back to BMC...")
    _slog("Exiting system console (Ctrl+D) after capture")
    channel.send("\x04")  # Ctrl+D
    time.sleep(2)
    output = direct_read_until(channel, ">", timeout=15)
    if ">" in output and "::" not in output[-10:]:
        print("   ✅ Returned to BMC prompt.")
        _slog("Returned to BMC prompt after capture")
    else:
        channel.send("\x04")
        time.sleep(1)
        direct_read_until(channel, ">", timeout=10)
        print("   ⚠️  BMC prompt not cleanly detected; proceeding.")
        _slog("BMC prompt not cleanly detected after exit", prefix="WARN")

    _retained_cluster_name = cluster_name
    _retained_net_config = net_rows
    _ctx_sync_from_globals()

    if _session_log:
        _session_log.end_phase()

    _print_retain_summary(cluster_name, net_rows, peer_addresses)
    return cluster_name, net_rows, peer_addresses


# ---------------------------------------------------------------------------
# Peer-node reset (mode 1): drive other BMCs to LOADER before option 9
# ---------------------------------------------------------------------------

def reset_peer_to_loader(host, username, password, timeout=600, node_log=None):
    """SSH to a peer BMC, run system reset, enter console, interrupt AUTOBOOT,
    and leave the node at the LOADER prompt. Returns True on success.

    When *node_log* is an open file handle, raw console output is written
    there instead of stdout; milestone status lines always go to the real
    terminal regardless, so parallel calls don't interleave console noise.
    """
    def _tprint(*args, **kwargs):
        """Print a milestone line directly to the real terminal."""
        print(*args, file=_real_stdout, **kwargs)
        _real_stdout.flush()

    _tprint(f"\n🔁 [{host}] Resetting to LOADER prompt...")
    _slog(f"Peer reset starting for {host}")

    client = None
    ch = None
    try:
        try:
            client, username, password = _ssh_connect_with_retry(
                host, username, password, label=f"peer/{host}",
                max_attempts=5, interactive=True,
            )
        except Exception as e:
            _tprint(f"   ❌ [{host}] Could not authenticate: {e}")
            if _session_log:
                _session_log.log(
                    f"Peer {host} auth/connect failed; skipping: {e}",
                    prefix="ERROR",
                )
            return False
        # Persist the (possibly updated) credentials so downstream steps
        # in this run reuse the working values for this BMC.
        _peer_bmc_creds[host] = {"user": username, "password": password}
        ch = _open_shell(client)

        # Reach BMC '>' prompt, taking over an existing session if needed.
        if not _reach_bmc_prompt(
            ch, node_log=node_log,
            takeover_msg=f"[{host}] taking over existing BMC session",
        ):
            _tprint(f"   ⚠️  [{host}] No BMC prompt; aborting peer reset.")
            _slog(f"[{host}] no BMC prompt; aborting", prefix="WARN")
            return False

        # system reset (auto-confirm).
        direct_send_and_wait(ch, "system reset", "y/n", timeout=15, auto_respond="y",
                             node_log=node_log, quiet=(node_log is not None))
        _tprint(f"\n   ⏳ [{host}] System reset in process — reboot will happen soon.")
        if _session_log:
            _session_log.log(
                f"[{host}] system reset issued; waiting for reboot to LOADER"
            )
        time.sleep(3)
        direct_read_until(ch, ">", timeout=20, node_log=node_log)

        # Enter system console.
        if _session_log:
            _session_log.log_sent("system console")
        ch.send("system console\r")
        out, matched = direct_read_until_any(
            ch,
            ["y/n", "ctrl-d", "type exit", "serial console", "boot loader", "loader", "autoboot"],
            timeout=15,
            node_log=node_log,
        )
        if matched and "y/n" in matched.lower():
            _slog(f"[{host}] taking over existing console session")
            ch.send("y\r")
            time.sleep(2)

        # Monitor for AUTOBOOT and LOADER.
        buf = ""
        start = time.monotonic()
        loader_seen = False
        while time.monotonic() - start < timeout:
            if _shutdown_event.is_set():
                break
            if ch.recv_ready():
                chunk = ch.recv(4096).decode("utf-8", errors="replace")
                buf += chunk
                if node_log:
                    _par_write(node_log, chunk)
                else:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                if _session_log:
                    _session_log.log_console(chunk)
                if "starting autoboot press ctrl-c to abort" in buf.lower():
                    _tprint(f"\n🛑 [{host}] AUTOBOOT detected; sending Ctrl+C...")
                    _slog(f"[{host}] AUTOBOOT detected; sending Ctrl+C")
                    if node_log:
                        _par_write(node_log, "\n>>> [Ctrl+C x5 — intercepting AUTOBOOT]\n")
                    for _ in range(5):
                        ch.send("\x03")
                        time.sleep(0.3)
                    buf = ""
                elif _LOADER_PROMPT_RE.search(buf):
                    loader_seen = True
                    break
                if len(buf) > 8192:
                    buf = buf[-4096:]
            time.sleep(0.1)

        if loader_seen:
            _tprint(f"   ✅ [{host}] At LOADER prompt. Disconnecting...")
            _slog(f"Peer {host} reached LOADER; disconnecting SSH and moving on")
        else:
            _tprint(f"   ⚠️  [{host}] Did not reach LOADER within {timeout}s.")
            _slog(f"Peer {host} did not reach LOADER (timeout)", prefix="WARN")

        # Closing the SSH session disconnects the BMC console takeover; no
        # need to send Ctrl+D here (which can stall at the LOADER prompt and
        # make the script appear hung). We just close and move on.
        return loader_seen
    except Exception as e:
        _tprint(f"   ❌ [{host}] Error during peer reset: {e}")
        _slog(f"Peer reset error on {host}: {e}", prefix="ERROR")
        return False
    finally:
        try:
            if ch is not None:
                ch.close()
        except Exception:
            pass
        try:
            if client is not None:
                client.close()
        except Exception:
            pass
        _slog(f"SSH to peer {host} closed")


# ---------------------------------------------------------------------------
# Interactive terminal
# ---------------------------------------------------------------------------

class InteractiveSession:
    def __init__(self, channel, client, sp_host, sp_user, sp_pass):
        self.channel = channel
        self.client = client
        self.sp_host = sp_host
        self.sp_user = sp_user
        self.sp_pass = sp_pass
        self._stop = threading.Event()

    def _try_reconnect(self):
        print("\n⚠️  SSH session dropped! The controller is still running.")
        print("🔄 Reconnecting to BMC and reattaching to console...")
        _slog("SSH dropped during interactive session, reconnecting")
        result = reconnect_to_sp(self.sp_host, self.sp_user, self.sp_pass)
        if result[0] is None:
            return False
        self.client, self.channel = result

        output, matched = direct_read_until_any(self.channel, ["y/n", ">"], timeout=15)
        if matched and "y/n" in matched.lower():
            print("⚠️  Existing session detected during reconnect, taking over...")
            if _session_log:
                _session_log.log("Auto-taking over existing session during reconnect")
                _session_log.log_sent("y")
            self.channel.send("y\r")
            time.sleep(2)
            direct_read_until(self.channel, ">", timeout=15)

        if _session_log:
            _session_log.log_sent("system console")
        self.channel.send("system console\r")
        time.sleep(1)
        output, matched = direct_read_until_any(
            self.channel,
            ["y/n", "ctrl-d", "type exit", "serial console", "boot loader", "loader", "autoboot"],
            timeout=15
        )
        if matched and "y/n" in matched.lower():
            print("⚠️  Existing console session detected during reconnect, taking over...")
            if _session_log:
                _session_log.log("Auto-taking over existing console session during reconnect")
                _session_log.log_sent("y")
            self.channel.send("y\r")
            time.sleep(2)

        drain_channel(self.channel, seconds=2)
        print("✅ Reattached to system console.\n")
        _slog("Reattached to system console after reconnect")
        return True

    def _reader_loop(self):
        while not self._stop.is_set() and not _shutdown_event.is_set():
            try:
                if not is_session_alive(self.client, self.channel):
                    if not self._try_reconnect():
                        break
                    continue
                if self.channel.recv_ready():
                    chunk = self.channel.recv(4096).decode("utf-8", errors="replace")
                    if chunk:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                        if _session_log:
                            _session_log.log_console(chunk)
                else:
                    time.sleep(0.1)
            except Exception as e:
                _slog(f"Reader error: {e}", prefix="ERROR")
                if not self._try_reconnect():
                    break

    def run(self):
        # Switch the node log writer to pass-through so the user sees all
        # console output and can type responses normally.
        _nlw = sys.stdout if isinstance(sys.stdout, _NodeLogWriter) else None
        if _nlw:
            _nlw.interactive = True

        print("\n📺 Session is now fully interactive.")
        print("   Type your responses to any prompts (yes, no, etc.)")
        print("   ⚠️  AUTOBOOT messages are NORMAL from this point – they will NOT be interrupted.")
        print("   Press Ctrl+C to exit. (Press twice to force exit.)\n")
        _slog("Entered interactive session (Phase 3 – passive mode)")

        reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        reader_thread.start()

        try:
            while not _shutdown_event.is_set():
                try:
                    user_input = input()
                    if _shutdown_event.is_set():
                        break
                    if _session_log:
                        _session_log.log_user_input(user_input)
                    if not is_session_alive(self.client, self.channel):
                        if not self._try_reconnect():
                            break
                    self.channel.send(user_input + "\r")
                    if _session_log:
                        _session_log.log_sent(user_input)
                except EOFError:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            reader_thread.join(timeout=0.5)
            # Restore filtering mode after interactive session ends.
            if _nlw:
                _nlw.interactive = False

        print("\n👋 Exiting interactive session.")
        _slog("Exited interactive session")


# ---------------------------------------------------------------------------
# Boot menu handler
# ---------------------------------------------------------------------------

def wait_for_boot_menu_and_select(channel, timeout=900, node_log=None):
    """Wait for the ONTAP boot menu then auto-select the configured option.

    When *node_log* is supplied all raw console bytes are written there
    instead of sys.stdout; only clean status lines appear on the terminal.
    """
    option, description = get_boot_menu_option()

    print(f"\n⏳ Primary node booting to boot menu (will auto-select option {option} – {description})...")
    if node_log and hasattr(node_log, "name"):
        print(f"   📝 Primary node log: {node_log.name}")
    if _session_log:
        _session_log.log(
            f"Phase 2: Waiting for boot menu up to {timeout}s "
            f"(will auto-select option {option} – {description})"
        )

    menu_signatures = [
        "selection (1-",   # "Selection (1-9)?" / "Selection (1-11)?"
        "(1-9)?",
        "(1-11)?",
        "(1-12)?",
    ]
    sig_lower = [s.lower() for s in menu_signatures]

    output = ""
    output_lower = ""
    start_time = time.monotonic()
    last_nudge = start_time
    last_progress = start_time

    while time.monotonic() - start_time < timeout:
        if _shutdown_event.is_set():
            return False
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            output_lower += chunk.lower()
            if node_log:
                _par_write(node_log, chunk)
            else:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            if _BMC_PROMPT_SIG in chunk.lower():
                _rc_t = time.monotonic()
                _reclaim_system_console(channel, node_log=node_log)
                start_time += time.monotonic() - _rc_t
                last_nudge = time.monotonic()
                output = ""
                output_lower = ""
                last_progress = time.monotonic()
                continue
            last_progress = time.monotonic()
            for sig in sig_lower:
                if sig in output_lower:
                    output_lower = ""  # found
                    break
            else:
                # cap buffer to avoid unbounded growth
                if len(output_lower) > 16384:
                    output_lower = output_lower[-8192:]
                time.sleep(0.1)
                continue
            break  # signature matched

        # If the console has been silent for ~30s, send a CR to nudge any
        # pending prompt that may already be displayed but waiting for input.
        now = time.monotonic()
        if now - last_progress > 30 and now - last_nudge > 30:
            channel.send("\r")
            last_nudge = now
            _slog("No console output for 30s; sending CR to nudge boot menu")
        time.sleep(0.1)
    else:
        print("⚠️  Boot menu prompt not detected within timeout.")
        if _session_log:
            _session_log.log(
                f"Boot menu prompt not detected within {timeout}s", prefix="WARN"
            )
        return False

    # Brief drain to let the trailing whitespace after "Selection (1-N)?" land.
    drain_channel(channel, seconds=1, node_log=node_log)

    if _operation_mode == 2:
        print(f"\n✅ Boot menu detected! Option {option} selected. Node reinitializing to be added to cluster.")
    else:
        print(f"\n✅ Boot menu detected! Option {option} selected. Node reinitializing and Storage Availability Zone being destroyed.")
    if _session_log:
        _session_log.log(f"Boot menu detected – auto-selecting option {option} ({description})")
        _session_log.log_sent(option)

    channel.send(option + "\r")
    time.sleep(2)

    # Check whether the menu is still sitting at "Selection (1-N)?" — if so,
    # the keystroke didn't land (slow console, race), so resend once.
    post = drain_channel(channel, seconds=2, node_log=node_log).lower()
    if any(s in post for s in sig_lower):
        if _session_log:
            _session_log.log(
                "Boot menu prompt still present after first send; retrying option",
                prefix="WARN",
            )
        print(f"   ↻ Menu prompt still visible; resending option {option}...")
        channel.send(option + "\r\n")
        time.sleep(2)

    return True


# ---------------------------------------------------------------------------
# LOADER / boot-menu handler
# ---------------------------------------------------------------------------

# Only this DNA value is supported by this script. Anything else indicates
# an unsupported platform/firmware combination and the operator must engage
# NetApp support before proceeding.
_REQUIRED_BOOT_DNA = "3088"


def _verify_boot_dna(channel):
    """Run 'printenv bootarg.init.dna' at the LOADER prompt and confirm the
    value is the supported DNA. Returns True if supported, False otherwise.
    """
    print("\n🧬 Verifying boot DNA (printenv bootarg.init.dna)...")
    _slog("Verifying boot DNA via 'printenv bootarg.init.dna'")

    output = direct_send_and_wait(
        channel, "printenv bootarg.init.dna", "LOADER", timeout=15
    )

    # Find the DNA value in the printenv output. Typical formats:
    #   bootarg.init.dna=3088
    #   bootarg.init.dna           3088
    # We restrict the gap between key and value to spaces/tabs (not \s, which
    # would span newlines) so the echoed command line "printenv
    # bootarg.init.dna\r\n" is NOT matched against the next line's "Variable"
    # table header. We then take the last match, which is the data row.
    dna_value = None
    matches = re.findall(
        r"bootarg\.init\.dna[ \t]*[=:]?[ \t]+(\S+)",
        output,
        flags=re.IGNORECASE,
    )
    # Reject obvious header/echo tokens.
    for candidate in reversed(matches):
        token = candidate.strip().rstrip(",;")
        if token and token.lower() not in {"value", "name", "variable"}:
            dna_value = token
            break

    if dna_value == _REQUIRED_BOOT_DNA:
        print(f"   ✅ Boot DNA = {dna_value} (supported). Continuing.")
        _slog(f"Boot DNA verified: {dna_value}")
        return True

    _print_banner("❌ UNSUPPORTED BOOT DNA")
    if dna_value is None:
        print("  Could not determine the boot DNA from 'printenv bootarg.init.dna'.")
    else:
        print(f"  Boot DNA reported: {dna_value}")
        print(f"  Required boot DNA: {_REQUIRED_BOOT_DNA}")
    print("")
    print("  This node is not configured as a NetApp AFX personality.")
    print("  Please contact NetApp Support before proceeding with reinitialization.")
    print("  Aborting script.")
    print("=" * 60 + "\n")
    if _session_log:
        _session_log.log(
            f"Unsupported boot DNA (got {dna_value!r}, required {_REQUIRED_BOOT_DNA}); "
            "aborting and instructing user to contact NetApp Support",
            prefix="ERROR",
        )
    return False


# ---------------------------------------------------------------------------
# Mode 1b: fully-automated post-option-9 cluster initialization
# ---------------------------------------------------------------------------

def _resolve_mgmt_lif_from_retained(lif_type: str):
    """Best-effort management LIF defaults from retained cluster data.

    ``lif_type`` is ``"node"`` or ``"cluster"``. The function matches rows
    whose ``role`` field equals ``<lif_type>-mgmt`` OR whose ``lif`` name
    contains both the type keyword and "mgmt". For node-mgmt, any row is
    used as a fallback when no explicit match is found.

    Returns ``{port, ip, netmask, gateway}`` with missing fields as None.
    """
    cfg = {"port": None, "ip": None, "netmask": None, "gateway": None}
    rows = _retained_net_config or []
    candidates = []
    for r in rows:
        role = (r.get("role") or "").lower()
        lif = (r.get("lif") or "").lower()
        if role == f"{lif_type}-mgmt" or (lif_type in lif and "mgmt" in lif):
            candidates.append(r)
    # node-mgmt: fall back to any row when no explicit match exists
    if not candidates and lif_type == "node" and rows:
        candidates = rows
    if candidates:
        r = candidates[0]
        cfg["port"] = r.get("home-port") or r.get("port")
        cfg["ip"] = r.get("address")
        cfg["netmask"] = r.get("netmask")
    cfg["gateway"] = _retained_default_gateway
    return cfg


def _resolve_node_mgmt_config_from_retained():
    """Wrapper: node-management LIF defaults for the primary BMC."""
    return _resolve_mgmt_lif_from_retained("node")


def _resolve_cluster_mgmt_from_retained():
    """Wrapper: cluster-management LIF defaults."""
    return _resolve_mgmt_lif_from_retained("cluster")




def apply_retained_to_cluster_config():
    """Merge values captured from the existing cluster (retain phase) into
    `_config_data["cluster"]` so they're treated as if they came from the
    JSON config file. Existing keys in the config are NEVER overwritten —
    operator-supplied / file-supplied values win, retained values only fill
    gaps. Returns the list of field names that were filled in.
    """
    cluster_block = _config_data.setdefault("cluster", {}) or {}
    _config_data["cluster"] = cluster_block

    cm = _resolve_cluster_mgmt_from_retained()

    fills = []

    def _fill(key, value):
        if value and not cluster_block.get(key):
            cluster_block[key] = value
            fills.append(key)

    _fill("name", _retained_cluster_name)
    _fill("clus_mgmt_address", cm.get("ip"))
    _fill("clus_mgmt_mask", cm.get("netmask"))
    _fill("clus_mgmt_gw", cm.get("gateway") or _retained_default_gateway)
    if cm.get("port"):
        _fill("mgmt_port", cm["port"])
    _fill("location", _retained_cluster_location)
    _fill("contact", _retained_cluster_contact)
    _fill("dns_domains", _retained_dns_domains)
    _fill("dns_servers", _retained_dns_servers)

    if fills:
        print("\n  \U0001F4D1 Populated cluster config from existing cluster:")
        for k in fills:
            shown = cluster_block[k]
            print(f"     {k:<20} = {shown}")
        if _session_log:
            _session_log.log(
                f"Populated cluster config from retained data: {fills}"
            )
    return fills


def _retained_node_mgmt_for(sp_address):
    """Look up the node-management LIF row for the ONTAP node owning
    ``sp_address``. Returns a dict {port, ip, netmask, gateway} with any
    missing fields set to None. Returns None if no mapping is available
    (no SP capture, or no node-mgmt LIF for that node).
    """
    if not sp_address:
        return None
    node_name = _retained_sp_to_node.get(sp_address)
    if not node_name:
        return None
    rows = _retained_net_config or []
    for r in rows:
        role = (r.get("role") or "").lower()
        lif = (r.get("lif") or "").lower()
        home_node = (r.get("home-node") or "").lower()
        if home_node != node_name.lower():
            continue
        if role == "node-mgmt" or ("node" in lif and "mgmt" in lif):
            return {
                "port": r.get("home-port") or r.get("port"),
                "ip": r.get("address"),
                "netmask": r.get("netmask"),
                "gateway": _retained_default_gateway,
            }
    return None


def apply_retained_to_node_configs(primary_bmc=None):
    """Populate per-BMC node-management fields in ``_config_data["nodes"]``
    from retained cluster data so the rest of the pipeline treats them as
    if they came from the JSON config.

    Each entry in ``_config_data["nodes"]`` is matched by ``bmc`` against
    the SP-address-to-node mapping captured during the retain phase. Any
    field already set in the config is left alone — retained values only
    fill gaps, exactly like ``apply_retained_to_cluster_config()``.

    The primary BMC (when known) is also added to
    ``_node_mgmt_by_bmc`` so it's available even if it's not listed in
    ``_config_data["nodes"]``.
    """
    fills_by_bmc = {}

    # Pre-seed _node_mgmt_by_bmc from every node block already in the config
    # so that values explicitly provided in the config file are available
    # to the rest of the pipeline even when retain capture fills nothing new.
    for _seed_node in ([_config_data.get("primary_node")]
                       + list(_config_data.get("secondary_nodes") or [])
                       + list(_config_data.get("nodes") or [])):
        if not isinstance(_seed_node, dict):
            continue
        _seed_bmc = (_seed_node.get("bmc") or "").strip()
        if not _seed_bmc or _seed_bmc in _node_mgmt_by_bmc:
            continue
        _port = (_seed_node.get("node_mgmt_port") or _seed_node.get("port"))
        _ip   = (_seed_node.get("node_mgmt_ip")   or _seed_node.get("ip"))
        _mask = (_seed_node.get("node_mgmt_netmask") or _seed_node.get("netmask"))
        _gw   = (_seed_node.get("node_mgmt_gateway") or _seed_node.get("gateway"))
        if any((_port, _ip, _mask, _gw)):
            _node_mgmt_by_bmc[_seed_bmc] = {
                "port": _port, "ip": _ip, "netmask": _mask, "gateway": _gw,
            }

    # Build a closure-style helper to fill one node entry / mgmt cache row.
    def _fill_node(bmc, mgmt):
        if not mgmt:
            return
        # Use _node_cfg_for to find the entry in whichever section it lives.
        node_block = _node_cfg_for(bmc)
        if not node_block:
            # Add a stub entry to the appropriate section.
            node_block = {"bmc": bmc}
            _pn = _config_data.get("primary_node")
            if isinstance(_pn, dict) and _pn.get("bmc") == bmc:
                pass  # already captured above but shouldn't happen
            elif bmc == primary_bmc and not _config_data.get("primary_node"):
                _config_data["primary_node"] = node_block
            elif isinstance(_config_data.get("secondary_nodes"), list):
                _config_data["secondary_nodes"].append(node_block)
            elif isinstance(_config_data.get("nodes"), list):
                # Preserve legacy nodes[] layout if the loaded config used it.
                _config_data["nodes"].append(node_block)
            else:
                # No existing node section — use secondary_nodes (current format).
                _config_data.setdefault("secondary_nodes", []).append(node_block)

        filled = []
        mapping = (
            ("node_mgmt_port", mgmt.get("port")),
            ("node_mgmt_ip", mgmt.get("ip")),
            ("node_mgmt_netmask", mgmt.get("netmask")),
            ("node_mgmt_gateway", mgmt.get("gateway")),
        )
        for key, value in mapping:
            if value and not node_block.get(key):
                node_block[key] = value
                filled.append(key)
        if filled:
            fills_by_bmc[bmc] = filled
            # Also seed _node_mgmt_by_bmc so the per-BMC collector can use
            # the values directly without re-reading the config block.
            _node_mgmt_by_bmc[bmc] = {
                "port": node_block.get("node_mgmt_port"),
                "ip": node_block.get("node_mgmt_ip"),
                "netmask": node_block.get("node_mgmt_netmask"),
                "gateway": node_block.get("node_mgmt_gateway"),
            }

    # Walk every SP address we captured (these are by definition cluster
    # peers, including the primary if SP enumeration includes it).
    for sp_addr in list(_retained_sp_to_node.keys()):
        mgmt = _retained_node_mgmt_for(sp_addr)
        if mgmt:
            _fill_node(sp_addr, mgmt)

    # Special case for the primary BMC when its SP address didn't appear
    # in the SP capture (some configurations exclude the local node).
    if primary_bmc and primary_bmc not in _retained_sp_to_node:
        primary_mgmt = _resolve_node_mgmt_config_from_retained()
        if any(primary_mgmt.values()):
            _fill_node(primary_bmc, primary_mgmt)

    if fills_by_bmc:
        print("\n  \U0001F4D1 Populated node-management config from existing cluster:")
        for bmc, keys in fills_by_bmc.items():
            node_block = _node_cfg_for(bmc) or {}
            shown = [f"{k}={node_block.get(k)}" for k in keys]
            print(f"     {bmc:<18} -> {', '.join(shown)}")
        if _session_log:
            _session_log.log(
                f"Populated per-node mgmt config from retained data: {fills_by_bmc}"
            )
    return fills_by_bmc


def write_config_snapshot(target_path):
    """Write the in-memory ``_config_data`` to ``target_path`` as JSON.

    Used after retain capture so the data scraped from the existing
    cluster ends up in the same on-disk config file the script would
    auto-detect on a future run, instead of only living in memory.

    Returns ``target_path`` on success, ``None`` on failure or when there
    is no config to write.
    """
    if not (_config_data.get("cluster") or _config_data.get("nodes")
            or _config_data.get("primary_node") or _config_data.get("secondary_nodes")):
        return None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target_path)) or ".",
                    exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(_config_data, f, indent=2, sort_keys=False)
            f.write("\n")
    except OSError as e:
        print(f"\n   ⚠️  Could not write config snapshot to {target_path}: {e}")
        if _session_log:
            _session_log.log(
                f"Config snapshot write failed ({target_path}): {e}",
                prefix="WARN",
            )
        return None
    print(f"\n   📄 Wrote config snapshot to {target_path}")
    _slog(f"Config snapshot written to {target_path}")
    return target_path


def _resolve_node_mgmt_config(bmc_host=None):
    """Return the node-management config to use for `bmc_host`.

    If the operator pre-populated values via `collect_node_mgmt_per_bmc()`,
    those are returned. Otherwise we fall back to retained-cluster defaults
    (legacy behaviour) so single-BMC mode 1b still works without the new
    pre-collection step.
    """
    if bmc_host and bmc_host in _node_mgmt_by_bmc:
        return dict(_node_mgmt_by_bmc[bmc_host])
    return _resolve_node_mgmt_config_from_retained()


def _prompt_with_default(label, default):
    """Prompt the user; pressing Enter accepts `default` (may be None)."""
    suffix = f" [{default}]" if default else ""
    value = _prompt(f"{label}{suffix}: ")
    return value or (default or None)


# Node-management input validators -----------------------------------------

# ONTAP port names: e<digits>[letter], e.g. e0M, e0a, e1a, e3b, e0M-1.
# Accept an optional trailing "-<digit>" for VLAN/sub-port forms.
_PORT_RE = re.compile(r"^e\d+[A-Za-z](-\d+)?$|^e\d+M(-\d+)?$")


def _is_valid_port(value):
    return bool(value) and bool(_PORT_RE.match(value.strip()))


def _is_valid_ipv4(value: str | None) -> bool:
    """Return True iff ``value`` is a dotted-quad IPv4 address with no
    leading-zero octets. Delegates to :mod:`ipaddress` for the core
    validation and adds the leading-zero check (which
    ``ipaddress.IPv4Address`` accepts in Python <3.9 but rejects later).
    """
    if not value:
        return False
    s = value.strip()
    parts = s.split(".")
    if len(parts) != 4:
        return False
    # Reject leading-zero octets ("01.2.3.4") explicitly so behavior is
    # consistent across Python versions.
    for p in parts:
        if not p.isdigit():
            return False
        if len(p) > 1 and p[0] == "0":
            return False
    try:
        ipaddress.IPv4Address(s)
    except (ipaddress.AddressValueError, ValueError):
        return False
    return True


def _prompt_validated(label, default, validator, error_hint):
    """Prompt with `_prompt_with_default` semantics, but re-prompt until
    `validator(value)` returns True. If `default` itself is invalid it is
    treated as no default and the user is forced to enter a valid value.
    """
    if default is not None and not validator(default):
        # Bad default (e.g. from a stale config); don't pre-fill it.
        default = None
    while True:
        value = _prompt_with_default(label, default)
        if value is None:
            # User pressed Enter with no default – force a real entry.
            print(f"    \u26A0\uFE0F  A value is required. {error_hint}")
            continue
        if validator(value):
            return value.strip()
        print(f"    \u26A0\uFE0F  Invalid value '{value}'. {error_hint}")


def _first_ipv4_in(text: str | None) -> str | None:
    """Return the first valid IPv4 address found in `text`, or None."""
    if not text:
        return None
    for m in re.finditer(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", text):
        candidate = m.group(1)
        if _is_valid_ipv4(candidate):
            return candidate
    return None


def _prompt_cluster_ip_fallback():
    """Manual fallback when we can't auto-discover a cluster-network IP."""
    print("\n  \u270F\uFE0F  Please enter a cluster-network IP manually.")
    while True:
        try:
            v = input("  Cluster-network IP (x.x.x.x): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not v:
            return None
        if _is_valid_ipv4(v):
            return v
        print("    \u26A0\uFE0F  Expected an IPv4 address in the form x.x.x.x.")


def _fetch_existing_cluster_ip(bmc_user=None, bmc_password=None):
    """Return an IP on the existing cluster's private cluster network by
    SSHing to the cluster management LIF and running
    `network interface show -role cluster -fields address`.

    Credential sources, tried silently in order:
      1. `_cluster_config` admin_user/admin_password (mode 1b path).
      2. The loaded JSON config file's `cluster.admin_user`/`admin_password`.
      3. The BMC creds passed in (typically what the operator entered when
         connecting to the primary BMC).

    Only when none of those authenticate (or none are available) do we
    prompt the operator. On any unrecoverable failure we fall back to
    asking for the cluster-network IP directly.
    """
    cfg_cluster = _config_data.get("cluster") or {}

    mgmt_ip = (_cluster_config.get("mgmt_ip")
               or cfg_cluster.get("clus_mgmt_address"))

    if not mgmt_ip:
        print("\n  \U0001F4E1 Need existing cluster details to look up a cluster-network IP.")
        mgmt_ip = _prompt("  Existing cluster management IP/hostname: ")
        if not mgmt_ip:
            return _prompt_cluster_ip_fallback()
        _cluster_config["mgmt_ip"] = mgmt_ip

    # Build the candidate list (user, password, source) preserving order and
    # dropping incomplete / duplicate pairs.
    candidates = _build_credential_list(
        (_cluster_config.get("admin_user"),
         _cluster_config.get("admin_password"),
         "previous cluster login"),
        (cfg_cluster.get("user") or "admin",
         cfg_cluster.get("password"),
         "config file"),
        (bmc_user, bmc_password, "BMC credentials"),
    )

    print(f"\n  \U0001F50C Connecting to existing cluster {mgmt_ip}...")
    _slog(f"Querying existing cluster {mgmt_ip} for cluster-network IP")

    client = None
    used_user = None
    used_pass = None
    try:
        # 1) Try every candidate silently first (no prompting).
        #    Use max_attempts=3 so brief cluster-startup delays don't cause
        #    an immediate fall-through to the interactive prompt.
        for user, password, source in candidates:
            try:
                client, used_user, used_pass = _ssh_connect_with_retry(
                    mgmt_ip, user, password,
                    label=f"cluster/{mgmt_ip}", max_attempts=3, interactive=False,
                )
                print(f"  ✅ Authenticated to {mgmt_ip} using {source} "
                      f"(user={used_user}).")
                if _session_log:
                    _session_log.log(
                        f"Cluster auth succeeded via {source} (user={used_user})"
                    )
                break
            except Exception as e:
                if _session_log:
                    _session_log.log(
                        f"Cluster auth via {source} failed: {e}", prefix="WARN"
                    )
                print(f"  ⚠️  {source}: {e}")
                client = None

        # 2) If no candidate worked, prompt the operator and retry with
        # interactive re-prompts on auth failure.
        if client is None:
            if candidates:
                print("\n  ⚠️  None of the known credentials worked for the cluster.")
            else:
                print("\n  ℹ️  No cluster credentials found in config or session state.")

            # Pre-fill defaults from known sources so the operator can just
            # press Enter if the config values are correct.
            _best_user = (
                _cluster_config.get("admin_user")
                or cfg_cluster.get("user")
                or (candidates[-1][0] if candidates else None)
                or "admin"
            )
            _best_pass = (
                _cluster_config.get("admin_password")
                or cfg_cluster.get("password")
            )
            _pass_hint = " [press Enter to use config password]" if _best_pass else ""
            try:
                entered_user = input(
                    f"  Cluster admin username [{_best_user}]: "
                ).strip() or _best_user
                entered_pass = getpass.getpass(
                    f"  Password for {entered_user}@{mgmt_ip}{_pass_hint}: "
                ) or _best_pass or ""
            except (EOFError, KeyboardInterrupt):
                return _prompt_cluster_ip_fallback()
            if not entered_pass:
                return _prompt_cluster_ip_fallback()

            try:
                client, used_user, used_pass = _ssh_connect_with_retry(
                    mgmt_ip, entered_user, entered_pass,
                    label=f"cluster/{mgmt_ip}", max_attempts=5, interactive=True,
                )
            except Exception as e:
                print(f"  \u274C Could not connect to cluster {mgmt_ip}: {e}")
                _slog(f"Cluster SSH failed: {e}", prefix="ERROR")
                return _prompt_cluster_ip_fallback()

        # Persist the working credentials for subsequent lookups.
        _cluster_config["admin_user"] = used_user
        _cluster_config["admin_password"] = used_pass

        for cmd in (
            "rows 0; network interface show -role cluster -fields address",
            "rows 0; net int show -role cluster -fields address",
        ):
            try:
                _, stdout, stderr = client.exec_command(cmd, timeout=30)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
            except Exception as e:
                _slog(f"Cluster cmd failed ({cmd}): {e}", prefix="WARN")
                continue
            _slog(f"$ {cmd}\n{out}{err}".rstrip())
            ip = _first_ipv4_in(out)
            if ip:
                print(f"  \u2705 Discovered cluster-network IP: {ip}")
                _slog(f"Discovered cluster-network IP: {ip}")
                return ip

        print("  \u26A0\uFE0F  Could not parse a cluster-network IP from cluster output.")
        _slog("Could not parse cluster-network IP from output", prefix="WARN")
        return _prompt_cluster_ip_fallback()
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass


def collect_node_mgmt_per_bmc(primary_bmc, peer_bmcs):
    """Prompt for node management port/IP/netmask/gateway for every BMC and
    populate `_node_mgmt_by_bmc`. Defaults are sourced from retained cluster
    data when available (only for the primary node; peers default to the
    primary's netmask/gateway since those are typically shared).
    """
    primary_defaults = _resolve_node_mgmt_config_from_retained()
    shared_netmask = primary_defaults.get("netmask")
    shared_gateway = primary_defaults.get("gateway") or _retained_default_gateway

    all_bmcs = [primary_bmc] + [b for b in peer_bmcs if b and b != primary_bmc]

    _print_banner("\U0001F310 Node Management Network Configuration")
    print("\n  Enter node-management network details for each BMC.")
    print("  These will be auto-answered during cluster setup (mode 1b).")
    print("  Press Enter to accept the [default] shown in brackets.")

    if _session_log:
        _session_log.start_phase("Collect Node Mgmt per BMC")
        _session_log.log(f"Collecting node mgmt config for BMCs: {all_bmcs}")

    for bmc in all_bmcs:
        is_primary = (bmc == primary_bmc)
        tag = "this node" if is_primary else "peer"
        print(f"\n  \u2500\u2500 BMC {bmc} ({tag}) \u2500\u2500")

        node_cfg = _node_cfg_for(bmc)

        if is_primary:
            d_port = node_cfg.get("node_mgmt_port") or primary_defaults.get("port") or "e0M"
            d_ip = node_cfg.get("node_mgmt_ip") or primary_defaults.get("ip")
            d_mask = node_cfg.get("node_mgmt_netmask") or primary_defaults.get("netmask") or shared_netmask
            d_gw = node_cfg.get("node_mgmt_gateway") or primary_defaults.get("gateway") or shared_gateway
        else:
            d_port = node_cfg.get("node_mgmt_port") or "e0M"
            d_ip = node_cfg.get("node_mgmt_ip")
            d_mask = node_cfg.get("node_mgmt_netmask") or shared_netmask
            d_gw = node_cfg.get("node_mgmt_gateway") or shared_gateway

        # If the config file has a complete AND valid entry for this BMC,
        # use it silently. Any invalid value forces the prompt path so the
        # operator can correct it before option 9/4 runs.
        cfg_port = node_cfg.get("node_mgmt_port")
        cfg_ip = node_cfg.get("node_mgmt_ip")
        cfg_mask = node_cfg.get("node_mgmt_netmask")
        cfg_gw = node_cfg.get("node_mgmt_gateway")
        config_valid = (
            _is_valid_port(cfg_port) and _is_valid_ipv4(cfg_ip)
            and _is_valid_ipv4(cfg_mask) and _is_valid_ipv4(cfg_gw)
        )

        if cfg_port and cfg_ip and cfg_mask and cfg_gw and config_valid:
            port = cfg_port
            ip = cfg_ip
            mask = cfg_mask
            gw = cfg_gw
            print(f"    \U0001F4C4 Using config file values for BMC {bmc}")
        else:
            if cfg_port or cfg_ip or cfg_mask or cfg_gw:
                if not config_valid:
                    print("    \u26A0\uFE0F  Config file values for this BMC are "
                          "incomplete or invalid; prompting for missing/invalid fields.")
                    if _session_log:
                        _session_log.log(
                            f"Config node-mgmt for {bmc} invalid/incomplete; "
                            f"prompting (port={cfg_port}, ip={cfg_ip}, "
                            f"mask={cfg_mask}, gw={cfg_gw})",
                            prefix="WARN",
                        )
            port = _prompt_validated(
                f"  Node management port for node with BMC {bmc}", d_port,
                _is_valid_port,
                "Expected an ONTAP port name like e0M, e0a, e1a, e3b.",
            )
            ip = _prompt_validated(
                f"  Node management address for node with BMC {bmc}", d_ip,
                _is_valid_ipv4,
                "Expected an IPv4 address in the form x.x.x.x (each octet 0-255).",
            )
            mask = _prompt_validated(
                f"  Node management netmask for node with BMC {bmc}", d_mask,
                _is_valid_ipv4,
                "Expected a dotted-quad netmask in the form x.x.x.x.",
            )
            gw = _prompt_validated(
                f"  Node management gateway for node with BMC {bmc}", d_gw,
                _is_valid_ipv4,
                "Expected an IPv4 address in the form x.x.x.x.",
            )

        _node_mgmt_by_bmc[bmc] = {
            "port": port, "ip": ip, "netmask": mask, "gateway": gw,
        }
        if _session_log:
            _session_log.log(
                f"Node mgmt for BMC {bmc}: port={port} ip={ip} "
                f"netmask={mask} gateway={gw}"
            )

    if _session_log:
        _session_log.end_phase()


def collect_cluster_config():
    """Gather cluster-level setup values used by mode 1b's wizard automation.

    Sources, in order of precedence:
      1. The loaded config file's `cluster` section.
      2. Retained data (cluster name only).
      3. Interactive prompts.

    Stores the resulting dict in the module-global `_cluster_config`.
    """
    cc_cfg = _config_data.get("cluster") or {}

    _print_banner("\U0001F3DB\uFE0F  Cluster Setup Configuration")
    print("\n  These values will be used to drive the cluster setup wizard")
    print("  after option 4 (mode 1b only). Press Enter to accept defaults.")

    if _session_log:
        _session_log.start_phase("Collect Cluster Setup Config")

    # Cluster name (default from retained capture if present).
    name_default = cc_cfg.get("name") or _retained_cluster_name
    if cc_cfg.get("name"):
        name = cc_cfg["name"]
        print(f"  \U0001F4C4 Cluster name (from config): {name}")
    else:
        name = _prompt_with_default("  Name of cluster", name_default)
        while not name:
            name = input("  Name of cluster (required): ").strip()

    # Admin password (always hidden). ONTAP requires the new cluster admin
    # password to contain BOTH letters and numbers (and be at least 8 chars
    # long). We enforce the same rule up-front so the wizard doesn't reject
    # the value mid-run.
    pw_rule_msg = ("ONTAP requires the cluster admin password to be at least "
                   "8 characters AND contain both letters and numbers.")

    def _password_ok(pw):
        return (isinstance(pw, str) and len(pw) >= 8
                and any(c.isalpha() for c in pw)
                and any(c.isdigit() for c in pw))

    # Honour a password pre-collected at the start of the run (e.g. when
    # BMC password was empty and the operator was prompted upfront).
    _pre_pw = _cluster_config.get("admin_password") if isinstance(_cluster_config, dict) else None
    admin_password = _pre_pw or cc_cfg.get("password")
    # Treat whitespace-only / non-string values as blank so the operator can
    # leave "password": "" (or omit it) in the config file and be prompted.
    if isinstance(admin_password, str) and not admin_password.strip():
        admin_password = None
    elif admin_password is not None and not isinstance(admin_password, str):
        admin_password = None
    if admin_password and _password_ok(admin_password):
        print("  \U0001F4C4 Cluster admin password loaded from config (hidden)")
    else:
        if admin_password and not _password_ok(admin_password):
            print(f"\n  \u26A0\uFE0F  Cluster admin password from config does NOT meet "
                  f"ONTAP requirements.\n     {pw_rule_msg}")
            if _session_log:
                _session_log.log(
                    "Config cluster.password rejected (must have letters AND "
                    "numbers, min 8 chars); prompting operator",
                    prefix="WARN",
                )
            admin_password = None
        print(f"\n  \u2139\uFE0F  {pw_rule_msg}")
        bmc_pw_available = bool(_primary_bmc_password)
        if bmc_pw_available:
            if _password_ok(_primary_bmc_password):
                print("  \U0001F4A1 Type 'bmc' to reuse the BMC login password "
                      "you already entered.")
            else:
                print("  \u2139\uFE0F  The BMC login password you already entered "
                      "does not meet the ONTAP rule above, so it cannot be "
                      "reused here.")
        while True:
            admin_password = getpass.getpass("  Admin password to use for cluster: ")
            if not admin_password:
                print("    \u26A0\uFE0F  Password cannot be empty.")
                continue
            if (bmc_pw_available
                    and admin_password.strip().lower() == "bmc"):
                if _password_ok(_primary_bmc_password):
                    admin_password = _primary_bmc_password
                    print("    \u2705 Reusing BMC login password as cluster admin "
                          "password (hidden).")
                    if _session_log:
                        _session_log.log(
                            "Operator reused BMC login password as cluster "
                            "admin password"
                        )
                    break
                print("    \u26A0\uFE0F  BMC password does not meet the ONTAP "
                      "requirement; please enter a new password.")
                continue
            if not _password_ok(admin_password):
                print(f"    \u26A0\uFE0F  Password does not meet requirements. "
                      f"{pw_rule_msg}")
                continue
            confirm = getpass.getpass("  Confirm admin password: ")
            if confirm != admin_password:
                print("    \u26A0\uFE0F  Passwords do not match. Try again.")
                continue
            break

    admin_user = cc_cfg.get("user") or "admin"
    if cc_cfg.get("user"):
        print(f"  \U0001F4C4 Cluster admin user (from config): {admin_user}")

    def _from_cfg_or_prompt(cfg_key, label, prompt_default, sensitive=False):
        """Use the config value silently when present; otherwise prompt."""
        val = cc_cfg.get(cfg_key)
        if val:
            shown = "<hidden>" if sensitive else val
            print(f"  \U0001F4C4 {label} (from config): {shown}")
            return val
        return _prompt_with_default(f"  {label}", prompt_default)

    # Cluster management port (e.g. e0M, e0c). The wizard refuses to
    # continue without a valid port name, so when the operator hasn't
    # supplied one in the config we prompt explicitly rather than guess.
    mgmt_port = cc_cfg.get("clus_mgmt_port") or cc_cfg.get("mgmt_port")
    if isinstance(mgmt_port, str):
        mgmt_port = mgmt_port.strip() or None
    if mgmt_port:
        print(f"  \U0001F4C4 Cluster management port (from config): {mgmt_port}")
    else:
        print("\n  \u2139\uFE0F  Cluster management port not in config (e.g. e0M, e0c).")
        while True:
            entry = _prompt("  Cluster management interface port: ")
            if entry:
                mgmt_port = entry
                break
            print("    \u26A0\uFE0F  Port cannot be empty.")

    mgmt_ip = _from_cfg_or_prompt(
        "clus_mgmt_address", "Cluster management IP", None)
    mgmt_netmask = _from_cfg_or_prompt(
        "clus_mgmt_mask", "Cluster management netmask",
        _resolve_node_mgmt_config_from_retained().get("netmask"))
    mgmt_gateway = _from_cfg_or_prompt(
        "clus_mgmt_gw", "Cluster management gateway",
        _retained_default_gateway)
    dns_domains = _from_cfg_or_prompt(
        "dns_domains", "DNS domain names (comma separated)", None)
    dns_servers = _from_cfg_or_prompt(
        "dns_servers", "DNS servers (comma separated)", None)
    location = _from_cfg_or_prompt(
        "location", "Controller location", None)

    _cluster_config.update({
        "name": name,
        "admin_user": admin_user,
        "admin_password": admin_password,
        "mgmt_port": mgmt_port,
        "mgmt_ip": mgmt_ip,
        "mgmt_netmask": mgmt_netmask,
        "mgmt_gateway": mgmt_gateway,
        "dns_domains": dns_domains,
        "dns_servers": dns_servers,
        "location": location,
    })

    if _session_log:
        # Don't write the password to the log.
        loggable = {k: v for k, v in _cluster_config.items() if k != "admin_password"}
        loggable["admin_password"] = "<hidden>"
        _session_log.log(f"Cluster setup config: {loggable}")
        _session_log.end_phase()


def _auto_answer_node_mgmt(channel, cfg, node_log=None):
    """Wait for each node-management setup prompt and answer it, falling back
    to interactive input() when the value isn't in the config.

    Uses a single carry-over buffer so data read while checking for a
    rejected value is NOT lost before the next prompt's scan begins.  This
    avoids the previous race where ONTAP's next prompt arrived inside the
    rejection-check window and was silently consumed, causing the subsequent
    direct_send_and_wait to wait 900 s for something it had already missed.
    """
    prompts = [
        ("port",    "node management interface port",            "node management interface port"),
        ("ip",      "node management interface ip address",      "node management interface IP address"),
        ("netmask", "node management interface netmask",         "node management interface netmask"),
        ("gateway", "node management interface default gateway", "node management interface default gateway"),
    ]

    # _buf accumulates raw channel output across all prompts.  It is only
    # replaced (with the rejection-check buffer) after a value is sent, so
    # any prompt text that arrived early is never discarded.
    _buf = ""
    pending = list(prompts)
    _overall_start = time.monotonic()
    _overall_timeout = 900

    while pending:
        if time.monotonic() - _overall_start > _overall_timeout:
            print("\n  ⚠️  Timed out waiting for node management prompts.")
            _slog("Timed out in _auto_answer_node_mgmt", prefix="WARN")
            break

        key, trigger, label = pending[0]
        trigger_lower = trigger.lower()

        print(f"\n⏳ Waiting for setup prompt: {label}...")
        _slog(f"Waiting for setup prompt: {label}")

        # Wait until the trigger appears in the accumulated buffer.
        _trigger_start = time.monotonic()
        while trigger_lower not in _buf.lower():
            if time.monotonic() - _trigger_start > _overall_timeout:
                break
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                _buf += chunk
                if node_log:
                    _par_write(node_log, chunk)
                else:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                if _session_log:
                    _session_log.log_console(chunk)
            time.sleep(0.1)

        # Prompt detected – resolve the value.
        value = cfg.get(key)
        if not value:
            value = _prompt(f"  Enter {label}: ")
            if _session_log:
                _session_log.log_user_input(f"{label} (manual): {value}")
        else:
            print(f"  ✅ Using config {label}: {value}")
            _slog(f"Auto-answering {label}: {value}")

        channel.send(value + "\r")
        if _session_log:
            _session_log.log_sent(value)

        # Rejection check: read for up to 5 s.  If the same prompt re-appears
        # the value was rejected.  The recheck output becomes the new _buf so
        # any text for the NEXT prompt that arrived in this window is not lost.
        time.sleep(0.3)
        _recheck = ""
        _recheck_start = time.monotonic()
        while time.monotonic() - _recheck_start < 5:
            if channel.recv_ready():
                rc = channel.recv(4096).decode("utf-8", errors="replace")
                _recheck += rc
                if node_log:
                    _par_write(node_log, rc)
                else:
                    sys.stdout.write(rc)
                    sys.stdout.flush()
                if _session_log:
                    _session_log.log_console(rc)
            else:
                time.sleep(0.1)

        if trigger_lower in _recheck.lower():
            # ONTAP re-prompted — the value was rejected.
            print(f"\n  ⚠️  Value '{value}' was rejected for {label}. Please re-enter.")
            if _session_log:
                _session_log.log(
                    f"{label}: value '{value}' rejected by ONTAP; prompting operator",
                    prefix="WARN",
                )
            cfg.pop(key, None)   # prevent the same bad value being reused
            value = _prompt(f"  Enter {label}: ")
            if _session_log:
                _session_log.log_user_input(f"{label} (corrected): {value}")
            channel.send(value + "\r")
            if _session_log:
                _session_log.log_sent(value)
            time.sleep(0.5)
            # Don't seed _buf with rejection text; start fresh for next prompt.
            _recheck = ""

        # Carry the recheck window output forward: the next prompt may already
        # be in it (ONTAP often sends prompts back-to-back).
        _buf = _recheck
        pending.pop(0)

    # Return the residual buffer so callers can detect prompts that arrived
    # while the last management value was being processed.
    return _buf


_WIZARD_START_TRIGGERS = [
    "press enter to complete cluster setup",
    "do you want to create a new cluster or join",
]

def _wait_for_wizard_start(channel, timeout=1800, node_log=None):
    """Wait for the ONTAP cluster-setup wizard to display its first prompt.

    Periodically sends CR (every 15 s of console silence) so the node sees
    activity and renders the prompt even if it is waiting for input.

    Returns the matched trigger string, or None on timeout.
    """
    triggers_lower = [t.lower() for t in _WIZARD_START_TRIGGERS]
    output = ""
    output_lower = ""
    start = time.monotonic()
    last_data = start
    last_nudge = start

    while time.monotonic() - start < timeout:
        if _shutdown_event.is_set():
            return None
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            output_lower += chunk.lower()
            if node_log:
                _par_write(node_log, chunk)
            else:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            if _BMC_PROMPT_SIG in chunk.lower():
                _rc_t = time.monotonic()
                _reclaim_system_console(channel, node_log=node_log)
                start += time.monotonic() - _rc_t
                output = ""
                output_lower = ""
                last_data = time.monotonic()
                last_nudge = time.monotonic()
                continue
            last_data = time.monotonic()
            for trigger, trigger_lower in zip(_WIZARD_START_TRIGGERS, triggers_lower):
                if trigger_lower in output_lower:
                    return trigger
            if len(output_lower) > 16384:
                output_lower = output_lower[-8192:]
        else:
            now = time.monotonic()
            if now - last_data > 15 and now - last_nudge > 15:
                channel.send("\r")
                last_nudge = now
                _slog("No wizard prompt seen for 15s; sending CR to nudge")
        time.sleep(0.1)
    return None


def _wait_and_send(channel, trigger, response, label, timeout=900, hide_in_log=False, quiet=False, node_log=None):
    """Wait for `trigger` substring (case-insensitive), then send `response`.
    Used by the cluster setup wizard automation.
    """
    if not quiet:
        print(f"\n⏳ Waiting for: {label}...")
    _slog(f"Waiting for: {label}")
    direct_send_and_wait(channel, "", trigger, timeout=timeout, check_bmc_drop=True, node_log=node_log)
    if response is None:
        response = ""
    channel.send(response + "\r")
    if _session_log:
        if hide_in_log:
            _session_log.log(f"[{label}] sent <hidden>")
        else:
            _session_log.log_sent(response if response else "<Enter>")
    time.sleep(0.5)


def _auto_answer_disk_erase_prompts(channel, node_log=None, label="", is_node_add=None):
    """Auto-answer the three disk-zero/erase/confirm prompts after option 4.

    ``is_node_add`` controls the progress messaging:
      * ``True``  -> peer is joining an existing cluster (mode 2a/2b, mode 3
                     peer-add phase, mode 2c resume).
      * ``False`` -> primary node is creating a new cluster (mode 1a/1b, mode
                     3 primary phase).
      * ``None``  -> fall back to the legacy ``_operation_mode == 2`` check.
                     This preserves prior behavior for any caller that hasn't
                     been updated yet, but new call sites should pass an
                     explicit value because ``_operation_mode == 3`` is
                     ambiguous (both primary init and peer add run under it).
    """
    _node_add = (_operation_mode == 2) if is_node_add is None else bool(is_node_add)
    if _node_add:
        _lbl = f" [{label}]" if label else ""
        print(f"\n⏳{_lbl} Resetting configuration and rebooting.")
    _cc_done_ev = None  # set when the cluster-creation progress reporter starts
    for trigger, resp, lbl in (
        ("zero disks, reset config and install a new file system", "yes",
         "zero disks confirmation"),
        ("this will erase all the data on the disks", "yes",
         "erase data confirmation"),
        ("type yes to confirm and continue", "yes",
         "type-yes confirmation"),
    ):
        if lbl == "type-yes confirmation":
            _log_path = _session_log.log_file if _session_log else "the log file"
            if _node_add:
                _boot_action = "join the cluster"
                _still_waiting_msg = "Still waiting for cluster join"
            else:
                _boot_action = "begin cluster creation"
                _still_waiting_msg = "Still waiting for cluster creation"
            print(f"\n⏳ Waiting for node to boot and {_boot_action}.")
            print(f"   For details, see log at:\n   {_log_path}\n   (open in a separate SSH session)")
            _cc_done_ev = threading.Event()
            _cc_t0 = time.monotonic()
            def _cc_reporter(_ev=_cc_done_ev, _t0=_cc_t0, _msg=_still_waiting_msg):
                while not _ev.wait(60):
                    elapsed = int(time.monotonic() - _t0)
                    print(f"   ⏳ {_msg}... ({elapsed}s elapsed)")
            threading.Thread(target=_cc_reporter, daemon=True).start()
        elif not _node_add:
            print(f"\n⏳ Waiting for {lbl} (auto-answer '{resp}')...")
        _slog(f"Waiting for {lbl}")
        direct_send_and_wait(channel, "", trigger, timeout=1800, auto_respond=resp,
                             check_bmc_drop=True, quiet=_node_add, node_log=node_log)
        if _cc_done_ev is not None:
            _cc_done_ev.set()
            _cc_done_ev = None


# ---------------------------------------------------------------------------
# ONTAP license helpers
# ---------------------------------------------------------------------------

def _collect_license_config(ctx):
    """Prompt the operator for ONTAP license details before the BMC session
    starts. Writes results into ``ctx`` and syncs back to the legacy
    ``_license_mode`` / ``_license_keys`` / ``_license_file_path`` globals
    so existing readers (e.g. ``_apply_license``) continue to work.
    """
    # Pull current globals into ctx so the trailing apply_to_globals() calls
    # don't clobber fields (e.g. _operation_mode, _auto_setup) that legacy
    # code updated since the ctx was last snapshotted.
    ctx.refresh_from_globals()
    _print_banner("\U0001f4dc ONTAP License")
    ans = _prompt(
        "\n  Add an ONTAP license (key or file)? [y/N]: "
    , "n").lower()
    if ans not in ("y", "yes"):
        return

    while True:
        ltype = _prompt("  License key or file? [key/file]: ").lower()
        if ltype in ("key", "file"):
            break
        print("  Please enter 'key' or 'file'.")

    # ── Key mode ──────────────────────────────────────────────────────────
    if ltype == "key":
        ctx.license_mode = "key"
        ctx.license_keys = []
        print("\n  Enter license keys one at a time. "
              "Press Enter on a blank line when done.")
        idx = 1
        while True:
            key = _prompt(f"  License key #{idx}: ")
            if not key:
                break
            ctx.license_keys.append(key)
            idx += 1
        if not ctx.license_keys:
            print("  \u26a0\ufe0f  No keys entered; license step will be skipped.")
            ctx.license_mode = None
        else:
            print(f"  \u2705 {len(ctx.license_keys)} license key(s) staged for post-setup apply.")

    # ── File mode ─────────────────────────────────────────────────────────
    else:
        ctx.license_mode = "file"
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ontap_dir = os.path.join(script_dir, "ONTAP")

        if not os.path.isdir(ontap_dir):
            # Create folder and instruct the operator to copy the file in.
            try:
                os.makedirs(ontap_dir, exist_ok=True)
                print(f"\n  \U0001f4c1 Created folder: {ontap_dir}")
            except OSError as exc:
                print(f"\n  \u274c Could not create ONTAP folder: {exc}")
                ctx.license_mode = None
                ctx.apply_to_globals()
                return
            print(
                "     Please exit the script, copy your ONTAP license file to\n"
                f"     that folder ({ontap_dir}),\n"
                "     and then run the script again."
            )
            ctx.apply_to_globals()
            sys.exit(0)

        # Folder exists — look for .txt / .nlf files only.
        _VALID_LIC_EXTS = {".txt", ".nlf"}
        lic_candidates = sorted(
            f for f in os.listdir(ontap_dir)
            if os.path.isfile(os.path.join(ontap_dir, f))
            and os.path.splitext(f)[1].lower() in _VALID_LIC_EXTS
        )

        def _prompt_custom_path():
            """Ask the operator for a manual file path; loop until valid or exit."""
            while True:
                custom = _prompt(
                    "  Enter the path to a valid license file or hit enter to exit the script: "
                )
                if not custom:
                    print("\n  Exiting script.")
                    ctx.apply_to_globals()
                    sys.exit(0)
                if not os.path.isfile(custom):
                    print(
                        f"  \u274c File not found: {custom}\n"
                        "     Please check the path and try again."
                    )
                    continue
                ext = os.path.splitext(custom)[1].lower()
                if ext not in {".txt", ".nlf"}:
                    print(
                        f"  \u274c '{os.path.basename(custom)}' does not have a "
                        "recognised license extension (.txt or .nlf).\n"
                        "     Please provide a .txt or .nlf file."
                    )
                    continue
                ctx.license_file_path = custom
                print(f"  \u2705 Using license file: {ctx.license_file_path}")
                break

        if lic_candidates:
            if len(lic_candidates) == 1:
                ctx.license_file_path = os.path.join(ontap_dir, lic_candidates[0])
                print(f"\n  \u2705 Found license file: {ctx.license_file_path}")
            else:
                print(f"\n  Multiple files found in {ontap_dir}:")
                for i, fn in enumerate(lic_candidates, 1):
                    print(f"    {i}. {fn}")
                while True:
                    choice = _prompt("  Select file number: ")
                    try:
                        n = int(choice)
                        if 1 <= n <= len(lic_candidates):
                            ctx.license_file_path = os.path.join(
                                ontap_dir, lic_candidates[n - 1]
                            )
                            break
                    except ValueError:
                        pass
                    print("  Invalid selection; please try again.")
                print(f"  \u2705 Using license file: {ctx.license_file_path}")
        else:
            # No .txt / .nlf files in the ONTAP folder.
            print(
                "\n  \u274c No valid license file found."
            )
            _prompt_custom_path()

    # Sync ctx writes back to the legacy globals so _apply_license and
    # _run_cluster_setup_wizard (which still read the globals) see them.
    ctx.apply_to_globals()


def _apply_license(channel):
    """Apply ONTAP licenses after cluster creation.

    Must be called while the console channel is logged in to the cluster
    shell (``::>`` prompt). Does nothing when _license_mode is None.
    """
    if not _license_mode:
        return

    admin_password = _cluster_config.get("admin_password") or ""

    _print_banner("\U0001f4dc Applying ONTAP License")
    if _session_log:
        _session_log.start_phase("License Application")
        _session_log.log(f"License mode: {_license_mode}")

    # Disable paging so long output isn't truncated.
    try:
        _run_cluster_command(channel, "rows 0", timeout=15)
    except Exception:
        pass

    # ── Key mode ──────────────────────────────────────────────────────────
    if _license_mode == "key":
        for key in _license_keys:
            display = key[:8] + "..." if len(key) > 8 else key
            print(f"\n  \U0001f511 Adding license key {display}")
            _slog(f"Adding license key ({display})")
            try:
                out = _run_cluster_command(
                    channel, f"license add -license-code {key}", timeout=30
                )
                if any(w in out.lower() for w in ("error", "failed", "invalid")):
                    print("  \u26a0\ufe0f  Response may indicate failure.")
                    if _session_log:
                        _session_log.log(
                            f"license add key {display} may have failed: "
                            f"{out[:200]}",
                            prefix="WARN",
                        )
                else:
                    print("  \u2705 Key accepted.")
            except Exception as exc:
                print(f"  \u274c Error adding key {display}: {exc}")
                if _session_log:
                    _session_log.log(
                        f"license add key {display} error: {exc}", prefix="ERROR"
                    )

    # ── File mode ─────────────────────────────────────────────────────────
    elif _license_mode == "file":
        # 1. Unlock diag account.
        print("\n  \U0001f513 Unlocking diag account...")
        _slog("Unlocking diag account (security login unlock -username diag)")
        try:
            _run_cluster_command(
                channel, "security login unlock -username diag", timeout=30
            )
        except Exception as exc:
            if _session_log:
                _session_log.log(
                    f"security login unlock failed: {exc}", prefix="WARN"
                )

        # 2. Set diag password = admin password.
        print("  \U0001f511 Setting diag account password...")
        _slog("Setting diag account password to match admin")
        try:
            drain_channel(channel, seconds=0.3)
            channel.send("security login password -username diag\r")
            if _session_log:
                _session_log.log_sent("security login password -username diag")
            _pw_already_correct = False
            for _pw_round in range(6):
                _out_pw, _match_pw = direct_read_until_any(
                    channel,
                    ["new password", "enter it again", "must be different",
                     "successfully changed", "::>", "::*>"],
                    timeout=20,
                )
                if not _match_pw:
                    break
                # Check the FULL output buffer first — "New password must be
                # different..." contains "new password" as a substring, so the
                # needle alone is not a reliable discriminator.
                _full_pw = (_out_pw + _match_pw).lower()
                if "must be different" in _full_pw or "successfully changed" in _full_pw:
                    # Diag password already matches admin; ONTAP rejected the
                    # change as identical.  This is the desired state — do not
                    # re-enter the password.
                    _pw_already_correct = True
                    direct_read_until_any(channel, ["::>", "::*>"], timeout=15)
                    break
                _ml = _match_pw.lower()
                if "::>" in _match_pw or "::*>" in _match_pw:
                    break
                if "new password" in _ml or "enter it again" in _ml:
                    channel.send(admin_password + "\r")
                    if _session_log:
                        _session_log.log_sent("<hidden>")
            if _pw_already_correct:
                print("  \u2139\ufe0f  Diag password already matches admin; no change needed.")
                if _session_log:
                    _session_log.log(
                        "Diag password unchanged "
                        "(ONTAP: new password must differ from old — already correct)"
                    )
            else:
                print("  \u2705 Diag account password set.")
        except Exception as exc:
            if _session_log:
                _session_log.log(
                    f"security login password failed: {exc}", prefix="WARN"
                )

        # 3. Enable systemshell diag login on all nodes.
        print("  \U0001f527 Enabling systemshell diag login on all nodes...")
        if _session_log:
            _session_log.log(
                'set diag -c off; system node systemshell -node * -command '
                '"sudo kenv bootarg.login.allowdiag=true"'
            )
        try:
            drain_channel(channel, seconds=0.3)
            channel.send(
                'set diag -c off; system node systemshell -node * -command '
                '"sudo kenv bootarg.login.allowdiag=true"\r'
            )
            if _session_log:
                _session_log.log_sent(
                    'set diag -c off; system node systemshell -node * -command '
                    '"sudo kenv bootarg.login.allowdiag=true"'
                )
            _kenv_deadline = time.monotonic() + 120
            while time.monotonic() < _kenv_deadline:
                _ko, _km = direct_read_until_any(
                    channel,
                    ["are you sure you want to continue",
                     "password:", "::>", "::*>"],
                    timeout=min(20, max(1, _kenv_deadline - time.monotonic())),
                )
                if not _km:
                    break
                _kml = _km.lower()
                if "are you sure you want to continue" in _kml:
                    channel.send("yes\r")
                    if _session_log:
                        _session_log.log_sent("yes  (SSH host key confirmation)")
                elif "password:" in _kml:
                    channel.send(admin_password + "\r")
                    if _session_log:
                        _session_log.log_sent("<hidden>  (diag systemshell password)")
                else:
                    break  # cluster prompt — done
        except Exception as exc:
            if _session_log:
                _session_log.log(
                    f"systemshell kenv allowdiag failed: {exc}", prefix="WARN"
                )

        # 4. Gather node-management IPs.
        print("  \U0001f50d Gathering node management IPs...")
        if _session_log:
            _session_log.log(
                "net int show -role node-mgmt -fields address (gather node IPs)"
            )
        node_ips = []
        try:
            ni_out = _run_cluster_command(
                channel,
                "net int show -role node-mgmt -fields address",
                timeout=30,
            )
            for line in ni_out.splitlines():
                for token in line.split():
                    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", token):
                        if token not in node_ips:
                            node_ips.append(token)
            if node_ips:
                print(f"  \u2705 Node-mgmt IPs: {', '.join(node_ips)}")
                _slog(f"Node-mgmt IPs: {node_ips}")
            else:
                print(
                    "  \u26a0\ufe0f  No node-mgmt IPs found; SFTP step will be skipped."
                )
                _slog("No node-mgmt IPs found", prefix="WARN")
        except Exception as exc:
            print(f"  \u26a0\ufe0f  net int show failed: {exc}")
            _slog(f"net int show failed: {exc}", prefix="WARN")

        # 5. Upload license file to each node via SFTP (diag account).
        #    Target: /droot/etc/lic_file  (writable from diag systemshell).
        #    After upload we cp it to /mroot/etc/lic_file from the cluster shell.
        _nodes_with_file = []   # node IPs where the upload + cp succeeded
        if node_ips and _license_file_path:
            lic_name = os.path.basename(_license_file_path)
            for node_ip in node_ips:
                print(
                    f"\n  \U0001f4e6 Copying {lic_name} "
                    f"\u2192 diag@{node_ip}:/droot/etc/lic_file"
                )
                if _session_log:
                    _session_log.log(
                        f"SFTP {_license_file_path} -> "
                        f"{node_ip}:/droot/etc/lic_file"
                    )
                _sftp_ok = False
                try:
                    lic_client = paramiko.SSHClient()
                    lic_client.set_missing_host_key_policy(
                        paramiko.AutoAddPolicy()
                    )
                    lic_client.connect(
                        node_ip,
                        username="diag",
                        password=admin_password,
                        timeout=30,
                        allow_agent=False,
                        look_for_keys=False,
                    )
                    sftp = lic_client.open_sftp()
                    # Track transfer progress to confirm 100 % completion.
                    _sftp_transferred = [0]
                    _sftp_total = [0]
                    def _sftp_progress(xfer, total):
                        _sftp_transferred[0] = xfer
                        _sftp_total[0] = total
                    sftp.put(_license_file_path, "/droot/etc/lic_file",
                             callback=_sftp_progress)
                    sftp.close()
                    lic_client.close()
                    # Verify 100 % transferred.
                    if (_sftp_total[0] > 0 and
                            _sftp_transferred[0] >= _sftp_total[0]):
                        _sftp_ok = True
                        print(
                            f"  \u2705 Uploaded to {node_ip} "
                            f"({_sftp_transferred[0]} / {_sftp_total[0]} bytes, 100%)"
                        )
                        if _session_log:
                            _session_log.log(
                                f"License file uploaded to {node_ip} via SFTP "
                                f"({_sftp_transferred[0]}/{_sftp_total[0]} bytes)"
                            )
                    else:
                        print(
                            f"  \u26a0\ufe0f  SFTP to {node_ip} completed but "
                            f"transfer looks incomplete "
                            f"({_sftp_transferred[0]}/{_sftp_total[0]} bytes)."
                        )
                        if _session_log:
                            _session_log.log(
                                f"SFTP to {node_ip}: incomplete transfer "
                                f"({_sftp_transferred[0]}/{_sftp_total[0]})",
                                prefix="WARN",
                            )
                except Exception as exc:
                    print(f"  \u274c SFTP to {node_ip} failed: {exc}")
                    if _session_log:
                        _session_log.log(
                            f"License SFTP to {node_ip} failed: {exc}",
                            prefix="ERROR",
                        )

                # 5b. If upload succeeded, cp from /droot to /mroot via cluster shell.
                if _sftp_ok:
                    _cp_cmd = (
                        'set diag -c off; system node systemshell -node * '
                        '-command "sudo cp /droot/etc/lic_file /mroot/etc/lic_file"'
                    )
                    print(f"  \U0001f4c2 Copying /droot/etc/lic_file \u2192 /mroot/etc/lic_file on {node_ip}...")
                    _slog(_cp_cmd)
                    try:
                        drain_channel(channel, seconds=0.3)
                        channel.send(_cp_cmd + "\r")
                        if _session_log:
                            _session_log.log_sent(_cp_cmd)
                        _cp_deadline = time.monotonic() + 60
                        while time.monotonic() < _cp_deadline:
                            _cp_o, _cp_m = direct_read_until_any(
                                channel,
                                ["are you sure you want to continue",
                                 "password:", "::>", "::*>"],
                                timeout=min(15, max(1, _cp_deadline - time.monotonic())),
                            )
                            if not _cp_m:
                                break
                            _cml = _cp_m.lower()
                            if "are you sure you want to continue" in _cml:
                                channel.send("yes\r")
                                if _session_log:
                                    _session_log.log_sent("yes  (SSH host key confirmation)")
                            elif "password:" in _cml:
                                channel.send(admin_password + "\r")
                                if _session_log:
                                    _session_log.log_sent("<hidden>  (diag systemshell password)")
                            else:
                                break
                        print(f"  \u2705 /mroot/etc/lic_file in place on {node_ip}.")
                        _slog(f"cp /droot -> /mroot done on {node_ip}")
                        _nodes_with_file.append(node_ip)
                    except Exception as exc:
                        print(f"  \u274c cp failed on {node_ip}: {exc}")
                        if _session_log:
                            _session_log.log(
                                f"cp /droot -> /mroot failed on {node_ip}: {exc}",
                                prefix="ERROR",
                            )

        # 6. Apply license from the uploaded file (only if at least one node has it).
        if _nodes_with_file:
            print("\n  \U0001f4dc Running 'set diag -c off; system license add -use-license-file true'...")
            _slog("Running set diag -c off; system license add -use-license-file true")
            try:
                _run_cluster_command(
                    channel,
                    "set diag -c off; system license add -use-license-file true",
                    timeout=60,
                )
            except Exception as exc:
                if _session_log:
                    _session_log.log(
                        f"set diag -c off; system license add -use-license-file true failed: {exc}",
                        prefix="ERROR",
                    )
        elif node_ips and _license_file_path:
            print(
                "\n  \u26a0\ufe0f  No nodes received the license file; "
                "skipping 'system license add'."
            )
            if _session_log:
                _session_log.log(
                    "system license add skipped: no successful uploads",
                    prefix="WARN",
                )

        # 7. Verify with license show.
        print("  \U0001f50d Running 'license show'...")
        _slog("Running license show to verify")
        licenses_applied = False
        try:
            lic_show_out = _run_cluster_command(
                channel, "license show", timeout=30
            )
            if "this table is currently empty" in lic_show_out.lower():
                print(
                    "  \u26a0\ufe0f  'license show' reports no licenses "
                    "(table empty \u2014 license file may not have applied)."
                )
                if _session_log:
                    _session_log.log(
                        "license show: table empty – license file may not "
                        "have worked",
                        prefix="WARN",
                    )
            else:
                print("  \u2705 Licenses present per 'license show'.")
                licenses_applied = True
                _slog("license show: licenses found")
        except Exception as exc:
            _slog(f"license show failed: {exc}", prefix="WARN")

        # 8. Cleanup: revoke systemshell access and re-lock diag account.
        if licenses_applied:
            print(
                "\n  \U0001f512 Revoking systemshell diag access and "
                "locking account..."
            )
            try:
                drain_channel(channel, seconds=0.3)
                channel.send(
                    'set diag -c off; system node systemshell -node * -command '
                    '"sudo kenv -u bootarg.login.allowdiag"\r'
                )
                if _session_log:
                    _session_log.log_sent(
                        'set diag -c off; system node systemshell -node * -command '
                        '"sudo kenv -u bootarg.login.allowdiag"'
                    )
                _kenv_u_deadline = time.monotonic() + 120
                while time.monotonic() < _kenv_u_deadline:
                    _ku_o, _ku_m = direct_read_until_any(
                        channel,
                        ["are you sure you want to continue",
                         "password:", "::>", "::*>"],
                        timeout=min(20, max(1, _kenv_u_deadline - time.monotonic())),
                    )
                    if not _ku_m:
                        break
                    _kuml = _ku_m.lower()
                    if "are you sure you want to continue" in _kuml:
                        channel.send("yes\r")
                        if _session_log:
                            _session_log.log_sent("yes  (SSH host key confirmation)")
                    elif "password:" in _kuml:
                        channel.send(admin_password + "\r")
                        if _session_log:
                            _session_log.log_sent("<hidden>  (diag systemshell password)")
                    else:
                        break  # cluster prompt — done
                _slog("systemshell kenv -u cleanup done")
            except Exception as exc:
                if _session_log:
                    _session_log.log(
                        f"systemshell kenv -u failed: {exc}", prefix="WARN"
                    )
            try:
                _run_cluster_command(
                    channel,
                    "security login lock -username diag",
                    timeout=30,
                )
                _slog("diag account locked")
            except Exception as exc:
                if _session_log:
                    _session_log.log(
                        f"security login lock failed: {exc}", prefix="WARN"
                    )
            print("  \u2705 diag account re-locked.")

    if _session_log:
        _session_log.end_phase()



# ---------------------------------------------------------------------------
# Mode 42 (4b): Netboot and install ONTAP
# ---------------------------------------------------------------------------

def _classify_auth_failure(exc: BaseException) -> str:
    """Return an operator-friendly description of why a BMC SSH auth/verify
    attempt failed. Recognized categories:

      * "Ping failed (host unreachable / no route / timed out)"
      * "SSH connection reset by peer (BMC SSH temporarily not responding)"
      * "Incorrect username or password (permission denied)"
      * "SSH banner not received (BMC SSH may still be starting up)"
      * Otherwise: the underlying exception text.
    """
    msg = str(exc).lower()
    # Auth-specific errors first (paramiko surfaces these explicitly).
    if isinstance(exc, paramiko.AuthenticationException):
        return "Incorrect username or password (permission denied)"
    if ("authentication" in msg or "permission denied" in msg
            or "bad authentication" in msg or "auth failed" in msg):
        return "Incorrect username or password (permission denied)"
    # Connection reset → BMC SSH temporarily offline.
    if (isinstance(exc, ConnectionResetError)
            or "connection reset" in msg or "reset by peer" in msg):
        return "SSH connection reset by peer (BMC SSH temporarily not responding)"
    # Host unreachable / ping-style failures.
    if isinstance(exc, (socket.gaierror, socket.timeout, TimeoutError)):
        return "Ping failed (host unreachable / DNS / timeout)"
    if ("timed out" in msg or "timeout" in msg or "unreachable" in msg
            or "no route" in msg or "connection refused" in msg
            or "name or service not known" in msg
            or "could not resolve" in msg or "host is down" in msg):
        return "Ping failed (host unreachable / no route / timed out)"
    # SSH banner errors.
    if "banner" in msg:
        return "SSH banner not received (BMC SSH may still be starting up)"
    # Fallback to raw exception text.
    return str(exc) or exc.__class__.__name__


def _verify_bmc_list_with_retries(bmc_ips, bmc_user, bmc_passwords,
                                  max_attempts=3, retry_pause=5):
    """Verify a list of BMCs via `_verify_bmc_ip`, retrying any that fail
    up to `max_attempts` times (default 3). Returns (ok, bmc_user) — ok
    is True if every BMC eventually verified, and bmc_user reflects any
    new shared username the operator entered while re-credentialing.
    Only the still-failing IPs are re-attempted on each retry, and if
    failures look like auth errors the operator is asked to re-enter
    credentials for those IPs before the next attempt (rather than
    silently retrying with the same bad password).

    `bmc_passwords` is updated in place as the operator enters new
    passwords for failing IPs.
    """
    pending = list(bmc_ips)
    # Reasons (per IP) from the most recent attempt — used to decide
    # whether to re-prompt for credentials before the next retry.
    last_reasons: "dict[str, str]" = {}

    def _is_auth_reason(reason):
        if not reason:
            return False
        r = reason.lower()
        return ("permission denied" in r
                or "incorrect username" in r
                or "incorrect password" in r
                or "authentication" in r)

    for _attempt in range(1, max_attempts + 1):
        if _attempt == 1:
            print("\n  Verifying BMC IP addresses via 'bmc status'...")
        else:
            # ── Credential re-prompt for auth failures ────────────────
            _auth_ips = [ip for ip in pending
                         if _is_auth_reason(last_reasons.get(ip))]
            if _auth_ips:
                print(
                    f"\n  🔐 {len(_auth_ips)} BMC(s) failed with an "
                    "auth/permission error. Re-enter credentials before "
                    "retrying:"
                )
                # Offer to share a single new (user, password) across all
                # the auth-failing BMCs to keep the prompt short.
                _same = _prompt(
                    "    Use the same new username/password for all "
                    "failing BMCs? [Y/n]: "
                , "n").lower()
                if _same != "n":
                    _new_user = input(
                        f"    BMC username [{bmc_user}] (blank to keep): "
                    ).strip() or bmc_user
                    _new_pass = getpass.getpass(
                        f"    BMC password for {_new_user}: "
                    )
                    if _new_pass:
                        bmc_user = _new_user
                        for _ip in _auth_ips:
                            bmc_passwords[_ip] = _new_pass
                else:
                    for _ip in _auth_ips:
                        print(f"    Credentials for {_ip}:")
                        _u = input(
                            f"      Username [{bmc_user}] (blank to keep): "
                        ).strip() or bmc_user
                        _p = getpass.getpass(
                            f"      Password for {_u}@{_ip}: "
                        )
                        if _p:
                            # Per-IP username override is not supported
                            # downstream; warn if the operator changes it.
                            if _u != bmc_user:
                                print(
                                    f"      ℹ️  Note: a single username is "
                                    f"used for all BMCs. '{_u}' will replace "
                                    f"'{bmc_user}' for every BMC."
                                )
                                bmc_user = _u
                            bmc_passwords[_ip] = _p
            print(
                f"\n  ↻ Retrying verification for {len(pending)} BMC(s) "
                f"(attempt {_attempt}/{max_attempts})..."
            )
            # Brief pause between attempts so a transient BMC blip can clear.
            time.sleep(retry_pause)

        next_pending = []
        last_reasons = {}
        for _ip in pending:
            ok, _, reason = _verify_bmc_ip(_ip, bmc_user, bmc_passwords[_ip])
            if ok:
                print(f"  ✅ {_ip} verified.")
            else:
                print(f"  ❌ {_ip} verification failed.")
                next_pending.append(_ip)
                last_reasons[_ip] = reason or "unknown failure"
        pending = next_pending
        if not pending:
            return True, bmc_user
    print(
        f"\n  ⚠️  {len(pending)} BMC(s) still failed verification after "
        f"{max_attempts} attempts: {', '.join(pending)}"
    )
    return False, bmc_user


def _verify_bmc_ip(ip, username, password):
    """SSH to `ip` and run 'bmc status', then confirm the reported IP Address
    matches what was entered. Returns (ok: bool, reported_ip: str|None,
    reason: str|None). `reason` is None on success, otherwise an
    operator-friendly description of why verification failed.
    """
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=ip, username=username, password=password,
                       timeout=15, banner_timeout=20)
        _stdin, stdout, _stderr = client.exec_command("bmc status")
        output = stdout.read().decode("utf-8", errors="replace")
        client.close()
        m = re.search(r'IP\s+Address:\s+(\S+)', output, re.IGNORECASE)
        if m:
            reported = m.group(1)
            if reported == ip:
                return True, reported, None
            print(f"    ⚠️  IP mismatch: entered {ip}, BMC reports {reported}")
            return False, reported, f"IP mismatch (BMC reports {reported})"
        print(f"    ⚠️  'IP Address:' not found in 'bmc status' output for {ip}.")
        print(f"       Output snippet: {output[:300].strip()!r}")
        return False, None, "'IP Address:' not found in 'bmc status' output"
    except Exception as exc:
        _reason = _classify_auth_failure(exc)
        print(f"    ⚠️  Cannot reach {ip}: {_reason}")
        if _session_log:
            _session_log.log(
                f"[BMC verify] {ip} failed: {_reason} (raw: {exc})",
                prefix="ERROR",
            )
        return False, None, _reason


def _collect_netboot_bmcs():
    """Interactive wizard: collect BMC IPs, credentials, optional JSON save,
    and verify each BMC via 'bmc status'.

    If a previously saved BMC config JSON is found, the operator is offered
    the option to load it instead of re-entering addresses.

    Returns (bmc_ips: list[str], bmc_user: str, bmc_passwords: dict{ip: str})
    or (None, None, None) on operator abort.
    """
    import json as _json

    # ── Fast-path: use already-loaded config data ──────────────────────────
    # If _config_data was populated from a config file before this function
    # was called, extract BMC addresses and credentials directly without
    # going through the interactive wizard.
    def _bmcs_from_data(data):
        """Return (bmc_ips, bmc_user, bmc_passwords) from a config dict,
        filtering by operation mode. Returns (None, None, None) if no BMCs."""
        _all_entries = []
        _pn = data.get("primary_node")
        _sns = data.get("secondary_nodes")
        _nodes = data.get("nodes")
        _has_new_fmt = isinstance(_pn, dict) or isinstance(_sns, list)

        if _has_new_fmt:
            if isinstance(_pn, dict) and _pn.get("bmc"):
                _all_entries.append(("primary", _pn))
            for _sn in (_sns or []):
                if isinstance(_sn, dict) and _sn.get("bmc"):
                    _all_entries.append(("secondary", _sn))
        elif isinstance(_nodes, list):
            for _i, _n in enumerate(_nodes):
                if isinstance(_n, dict) and _n.get("bmc"):
                    _all_entries.append(("primary" if _i == 0 else "secondary", _n))

        if not _all_entries:
            return None, None, None

        # Filter by role.
        if _operation_mode == 1:
            _filtered = [e for role, e in _all_entries if role == "primary"]
        elif _operation_mode == 2:
            _filtered = [e for role, e in _all_entries if role == "secondary"]
        else:
            _filtered = [e for _, e in _all_entries]

        if not _filtered:
            return None, None, None

        _ips = [str(e["bmc"]) for e in _filtered]
        _first_user = next((e.get("bmc_user") for e in _filtered if e.get("bmc_user")), None) or "admin"
        _pw_map = {str(e["bmc"]): e.get("bmc_password", "") for e in _filtered}
        return _ips, _first_user, _pw_map

    if isinstance(_config_data, dict) and (
        _config_data.get("primary_node") or _config_data.get("secondary_nodes") or _config_data.get("nodes")
    ):
        _ci_ips, _ci_user, _ci_pws = _bmcs_from_data(_config_data)
        if _ci_ips:
            print(f"\n  📄 Using BMC addresses from loaded config file ({len(_ci_ips)} node(s)):")
            for _ip in _ci_ips:
                print(f"     • {_ip}  (user={_ci_user})")
            return _ci_ips, _ci_user, _ci_pws

    # ── Look for existing BMC config files ────────────────────────────────
    _candidates = []
    try:
        _script_dir_nb = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        _script_dir_nb = os.getcwd()
    _configs_dir = os.path.join(_script_dir_nb, "configs")
    _search_dirs = [_configs_dir, _script_dir_nb]
    if os.path.abspath(os.getcwd()) not in [os.path.abspath(d) for d in _search_dirs]:
        _search_dirs.append(os.getcwd())
    _seen_paths = set()
    for _d in _search_dirs:
        if not os.path.isdir(_d):
            continue
        for _fname in sorted(os.listdir(_d)):
            if _fname.lower().endswith(".json"):
                _fpath = os.path.abspath(os.path.join(_d, _fname))
                if _fpath in _seen_paths:
                    continue
                try:
                    with open(_fpath, "r", encoding="utf-8") as _tf:
                        _data = _json.load(_tf)
                    # Accept files that contain a "netboot_bmcs" list, the
                    # new primary_node/secondary_nodes format, or the legacy
                    # "nodes" list (the main reinit config format).
                    _ips = None
                    if isinstance(_data.get("netboot_bmcs"), list):
                        _ips = [str(x) for x in _data["netboot_bmcs"] if x]
                    elif isinstance(_data.get("primary_node"), dict) or isinstance(_data.get("secondary_nodes"), list):
                        # New format: collect all BMC IPs from both sections.
                        _ips = []
                        _pn = _data.get("primary_node")
                        if isinstance(_pn, dict) and _pn.get("bmc"):
                            _ips.append(str(_pn["bmc"]))
                        for _sn in (_data.get("secondary_nodes") or []):
                            if isinstance(_sn, dict) and _sn.get("bmc"):
                                _ips.append(str(_sn["bmc"]))
                        _ips = _ips or None
                    elif isinstance(_data.get("nodes"), list):
                        _ips = [
                            str(n["bmc"]) for n in _data["nodes"]
                            if isinstance(n, dict) and n.get("bmc")
                        ]
                    if _ips:
                        _candidates.append((_fpath, _data, _ips))
                        _seen_paths.add(_fpath)
                except Exception:
                    pass

    if _candidates:
        # Auto-select BMC_IP.json if present.
        _auto_idx = next(
            (i for i, (fp, _, _) in enumerate(_candidates)
             if os.path.basename(fp) == "BMC_IP.json"),
            None,
        )

        if _auto_idx is not None:
            _fpath, _data, _bmc_ips = _candidates[_auto_idx]
            print(f"\n  ✅ Auto-selected {_fpath}  ({len(_bmc_ips)} node(s): {', '.join(_bmc_ips)})")
            # Use the file data directly — fall through to the credential
            # extraction block but bypass the interactive selection prompt.
            _sel = str(_auto_idx + 1)
        else:
            print("\n  Found existing BMC config file(s):")
            for _i, (_fpath, _data, _ips) in enumerate(_candidates, 1):
                print(f"    {_i}. {_fpath}  ({len(_ips)} node(s): {', '.join(_ips)})")
            print("    0. Enter BMC addresses manually")
            print("")
            while True:
                try:
                    _sel = input("  Load a config file? [1] or 0 for manual entry: ").strip()
                except (EOFError, KeyboardInterrupt):
                    return None, None, None
                if _sel == "" or _sel == "1" and len(_candidates) == 1:
                    _sel = "1"
                if _sel == "0":
                    break  # fall through to manual entry
                if _sel.isdigit() and 1 <= int(_sel) <= len(_candidates):
                    _fpath, _data, _bmc_ips = _candidates[int(_sel) - 1]
                    print(f"\n  ✅ Loaded {len(_bmc_ips)} BMC(s) from {_fpath}")
                    for _ip in _bmc_ips:
                        print(f"     • {_ip}")
                    break
                print("  ⚠️  Invalid selection.")

        if _auto_idx is not None or (_sel.isdigit() and int(_sel) > 0):
            # Filter the BMC list by role based on operation mode:
            #   modes 1a/1b   -> primary only
            #   mode  2a/2b   -> secondary only
            #   mode  3 / 42  -> all (primary first, then secondaries)
            # For new-format configs extract per-role; legacy uses position.
            _has_new_fmt = (isinstance(_data.get("primary_node"), dict)
                            or isinstance(_data.get("secondary_nodes"), list))
            if _has_new_fmt:
                _pn_bmc = (_data.get("primary_node") or {}).get("bmc")
                _sn_bmcs = [str(n["bmc"]) for n in (_data.get("secondary_nodes") or [])
                            if isinstance(n, dict) and n.get("bmc")]
                if _operation_mode in (1,):           # 1a / 1b
                    _role_ips = [_pn_bmc] if _pn_bmc else _bmc_ips
                elif _operation_mode == 2:            # 2a / 2b
                    _role_ips = _sn_bmcs if _sn_bmcs else _bmc_ips
                else:                                 # 3, 42, or unknown
                    _role_ips = ([_pn_bmc] if _pn_bmc else []) + _sn_bmcs or _bmc_ips
                _bmc_ips = [ip for ip in _role_ips if ip]
                if _bmc_ips and _bmc_ips != _role_ips:
                    print(f"\n  ℹ️  Filtered to {len(_bmc_ips)} BMC(s) for this operation mode.")
            elif _operation_mode == 2:
                # Legacy format: skip position-0 (primary); use rest as peers.
                _legacy_all = [str(n["bmc"]) for n in (_data.get("nodes") or [])
                               if isinstance(n, dict) and n.get("bmc")]
                if len(_legacy_all) > 1:
                    _bmc_ips = _legacy_all[1:]
                    print(f"\n  ℹ️  Mode 2: using {len(_bmc_ips)} secondary node(s) from config.")
            elif _operation_mode == 1:
                # Legacy format: use only position-0 as primary.
                _legacy_all = [str(n["bmc"]) for n in (_data.get("nodes") or [])
                               if isinstance(n, dict) and n.get("bmc")]
                if _legacy_all:
                    _bmc_ips = [_legacy_all[0]]
                    print("\n  ℹ️  Mode 1: using primary node only from config.")

            # Try to pull credentials from the file.
            _bmc_user = None
            _bmc_passwords = {}
            # Collect creds from all node entries (primary_node + secondary_nodes + legacy nodes).
            _all_node_entries = []
            _pn = _data.get("primary_node")
            if isinstance(_pn, dict):
                _all_node_entries.append(_pn)
            _all_node_entries.extend(n for n in (_data.get("secondary_nodes") or []) if isinstance(n, dict))
            _all_node_entries.extend(n for n in (_data.get("nodes") or []) if isinstance(n, dict))
            _first_user = None
            for _n in _all_node_entries:
                _u = _n.get("bmc_user", "")
                _p = _n.get("bmc_password", "")
                if _first_user is None:
                    _first_user = _u
                _bmc_ip = str(_n.get("bmc", ""))
                if _bmc_ip and _u:
                    _bmc_passwords[_bmc_ip] = _p
            if _first_user:
                _bmc_user = _first_user

            if not _bmc_user:
                _bmc_user = input("  BMC username: ").strip() or "admin"
            else:
                # Only prompt to override the username for address-only files
                # (BMC_IP.json). Full config files carry the username.
                _is_addr_only = isinstance(_data.get("netboot_bmcs"), list)
                if _is_addr_only:
                    _override = input(
                        f"  BMC username from file: {_bmc_user}  "
                        "(press Enter to accept or type a new one): "
                    ).strip()
                    if _override:
                        _bmc_user = _override
                else:
                    print(f"  📄 BMC username from config: {_bmc_user}")

            # Fill in any missing passwords.
            _missing = [_ip for _ip in _bmc_ips
                        if _ip not in _bmc_passwords or not _bmc_passwords[_ip]]
            _has_from_file = [_ip for _ip in _bmc_ips
                              if _ip not in _missing]

            if _missing:
                _same_ans = input(
                    f"\n  Password needed for {len(_missing)} BMC(s)."
                    "  Use the same password for all? [Y/n]: "
                ).strip().lower()
                if _same_ans != "n":
                    _shared_pass = getpass.getpass("  BMC password: ")
                    for _ip in _missing:
                        _bmc_passwords[_ip] = _shared_pass
                else:
                    for _ip in _missing:
                        _bmc_passwords[_ip] = getpass.getpass(
                            f"  Password for {_ip}: "
                        )

            for _ip in _has_from_file:
                # Only prompt to override if this came from a BMC_IP.json
                # (address-only file with no embedded creds). For full config
                # files that carry credentials, accept them silently.
                _is_addr_only = isinstance(_data.get("netboot_bmcs"), list)
                if _is_addr_only:
                    _override_p = input(
                        f"  Password for {_ip} loaded from file."
                        "  Press Enter to accept or type a new one: "
                    )
                    if _override_p:
                        _bmc_passwords[_ip] = _override_p

            # Verify (auto-retries each failing BMC up to 3 times before prompting).
            all_ok, _bmc_user = _verify_bmc_list_with_retries(
                _bmc_ips, _bmc_user, _bmc_passwords, max_attempts=3,
            )

            if all_ok:
                return _bmc_ips, _bmc_user, _bmc_passwords

            while True:
                retry = input(
                    "\n  One or more BMCs failed verification."
                    " Re-enter BMC addresses? [y/N]: "
                ).strip().lower()
                if retry == "y":
                    break  # fall through to manual entry
                if retry == "n" or retry == "":
                    return None, None, None
        # end if _candidates

    while True:  # outer retry loop on verification failure
        # ── IP collection ──────────────────────────────────────────────────
        bmc_ips = []
        print("\n  Enter the BMC IP address for each node.")
        print("  Press Enter on a blank line when done.")
        while True:
            ip = input(f"  BMC node {len(bmc_ips) + 1} IP address"
                       " (blank to finish): ").strip()
            if not ip:
                break
            bmc_ips.append(ip)

        if not bmc_ips:
            print("  No BMC IP addresses entered. Aborting.")
            return None, None, None

        # ── Optional JSON save ─────────────────────────────────────────────
        save_ans = input("\n  Save these BMC addresses to a JSON file for later"
                         " use? [y/N]: ").strip().lower()
        if save_ans == "y":
            os.makedirs(_configs_dir, exist_ok=True)
            default_path = os.path.join(_configs_dir, "BMC_IP.json")
            json_path = input(
                f"  JSON file path [{default_path}]: "
            ).strip() or default_path
            try:
                with open(json_path, "w", encoding="utf-8") as _f:
                    _json.dump({"netboot_bmcs": bmc_ips}, _f, indent=2)
                print(f"  ✅ Saved to {json_path}")
            except Exception as exc:
                print(f"  ⚠️  Could not save JSON: {exc}")

        # ── Credentials ────────────────────────────────────────────────────
        print("\n  BMC credentials:")
        same_ans = input("  Use the same username/password for all BMCs? [Y/n]: ").strip().lower()
        bmc_user = input("  BMC username: ").strip() or "admin"
        if same_ans != "n":
            bmc_pass = getpass.getpass("  BMC password: ")
            bmc_passwords = {ip: bmc_pass for ip in bmc_ips}
        else:
            bmc_passwords = {}
            for ip in bmc_ips:
                bmc_passwords[ip] = getpass.getpass(f"  Password for {ip}: ")

        # ── Verification (auto-retries each failing BMC up to 3 times) ─────
        all_ok, bmc_user = _verify_bmc_list_with_retries(
            bmc_ips, bmc_user, bmc_passwords, max_attempts=3,
        )

        if all_ok:
            return bmc_ips, bmc_user, bmc_passwords

        while True:
            retry = input("\n  One or more BMCs failed verification."
                          " Re-enter BMC addresses? [y/N]: ").strip().lower()
            if retry == "y":
                break  # re-collect
            if retry == "n" or retry == "":
                return None, None, None


def _bmc_reach_loader(host, username, password, timeout=600, node_log=None,
                      fallback_passwords=None):
    """SSH to a BMC, reset the node, enter system console, interrupt AUTOBOOT,
    and return (client, channel) with the channel *at the LOADER prompt*.
    The caller is responsible for closing client/channel.
    When *node_log* is supplied all raw console I/O is written there instead
    of sys.stdout; status milestones are always printed to the terminal.
    Returns (None, None) on failure.
    """
    print(f"\n  🔁 [{host}] Connecting and resetting to LOADER...")
    if node_log:
        _par_write(node_log, f"\n=== _bmc_reach_loader: {host} ===\n")
    try:
        client, username, password = _ssh_connect_with_retry(
            host, username, password, label=f"BMC/{host}",
            max_attempts=max(3, 1 + len(fallback_passwords or [])),
            interactive=False, fallback_passwords=fallback_passwords,
        )
    except Exception as exc:
        print(f"  ❌ [{host}] SSH failed: {exc}")
        return None, None

    try:
        ch = _open_shell(client)

        # Reach BMC '>' prompt.
        if not _reach_bmc_prompt(ch, node_log=node_log):
            print(f"  ❌ [{host}] No BMC prompt received.")
            ch.close()
            client.close()
            return None, None

        # system reset (auto-confirm if prompted).
        direct_send_and_wait(ch, "system reset", "y/n", timeout=15,
                             auto_respond="y", node_log=node_log)
        print(f"  ⏳ [{host}] Node rebooting...")
        time.sleep(3)
        direct_read_until(ch, ">", timeout=20, node_log=node_log)

        # system console.
        ch.send("system console\r")
        out2, matched2 = direct_read_until_any(
            ch,
            ["y/n", "ctrl-d", "serial console", "loader", "autoboot"],
            timeout=15,
            node_log=node_log,
        )
        if matched2 and "y/n" in matched2.lower():
            ch.send("y\r")
            time.sleep(2)

        # Monitor for AUTOBOOT interrupt and LOADER prompt.
        # Raw console output (BIOS init, driver load, etc.) goes to node_log
        # only; status milestones are printed to the terminal.
        print(f"  ⏳ [{host}] Waiting for AUTOBOOT / LOADER prompt...")
        buf = ""
        start = time.monotonic()
        loader_seen = False
        _next_progress = start + 30
        while time.monotonic() - start < timeout:
            if _shutdown_event.is_set():
                break
            now = time.monotonic()
            if now >= _next_progress:
                elapsed = int(now - start)
                print(f"  ⏳ [{host}] Still booting... ({elapsed}s elapsed)")
                _next_progress = now + 30
            if ch.recv_ready():
                chunk = ch.recv(4096).decode("utf-8", errors="replace")
                buf += chunk
                if node_log:
                    _par_write(node_log, chunk)
                else:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                if "starting autoboot press ctrl-c to abort" in buf.lower():
                    print(f"  🛑 [{host}] AUTOBOOT detected – sending Ctrl+C...")
                    if node_log:
                        _par_write(node_log, "\n>>> ^C (interrupting autoboot)\n")
                    for _ in range(6):
                        ch.send("\x03")
                        time.sleep(0.3)
                    buf = ""
                elif _LOADER_PROMPT_RE.search(buf):
                    loader_seen = True
                    break
                if len(buf) > 8192:
                    buf = buf[-4096:]
            time.sleep(0.1)

        if not loader_seen:
            print(f"  ❌ [{host}] LOADER prompt not detected within {timeout}s.")
            ch.close()
            client.close()
            return None, None

        print(f"  ✅ [{host}] At LOADER prompt.")
        return client, ch

    except Exception as exc:
        print(f"  ❌ [{host}] Error reaching LOADER: {exc}")
        try:
            ch.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        return None, None


# ---------------------------------------------------------------------------
# Per-node parallel I/O helpers
# Raw channel I/O that writes to a per-node file instead of stdout.
# Used by the 4b parallel workers so screen output stays clean.
# ---------------------------------------------------------------------------

def _par_write(nf, text):
    """Append raw console text to a per-node log file."""
    try:
        nf.write(text)
        nf.flush()
    except Exception:
        pass


def _par_recv_until(ch, nf, look_for_list, timeout=30):
    """Like direct_read_until_any but writes to *nf* (not stdout).

    Returns (output, matched_string_or_None).
    """
    look_for_lower = [s.lower() for s in look_for_list]
    output = ""
    output_lower = ""
    start_time = time.monotonic()
    while True:
        if _shutdown_event.is_set():
            return output, None
        if ch.recv_ready():
            chunk = ch.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            output_lower += chunk.lower()
            if nf:
                _par_write(nf, chunk)
            for look_for, lfl in zip(look_for_list, look_for_lower):
                if lfl in output_lower:
                    return output, look_for
        if time.monotonic() - start_time > timeout:
            return output, None
        time.sleep(0.1)


def _par_send(ch, nf, cmd):
    """Send *cmd* to channel; annotate with a marker line in *nf*."""
    if nf:
        _par_write(nf, f"\n>>> {cmd}\n")
    ch.send(cmd + "\r")


def _node_log_open(ip, log_dir, prefix="node"):
    """Open (or create) a per-node log file in *log_dir*.

    Returns an open file handle (text mode, line-buffered).
    """
    os.makedirs(log_dir, exist_ok=True)
    safe_ip = ip.replace(".", "_").replace(":", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, f"{prefix}_{safe_ip}_{ts}.log")
    return open(path, "w", encoding="utf-8", buffering=1)


def _run_netboot_install_sequence(channel, pkg_url, node_label="node",
                                  log=None, boot_menu_timeout=900,
                                  node_file=None, status_cb=None,
                                  static_ifconfig=None):
    """Run the netboot install sequence on a channel already at LOADER prompt.

    *static_ifconfig* — when supplied, a dict with keys ``port``, ``ip``,
    ``netmask``, and ``gateway`` used to configure the LOADER interface
    statically (``priv set diag`` + ``ifconfig <port> -addr=… -mask=… -gw=…``)
    instead of the default ``ifconfig e0M -auto``.

    Steps:
      1. ifconfig (DHCP -auto or static via priv set diag)
      2. netboot <pkg_url>
      3. Wait for boot menu → select option 7
      4. Answer all install prompts
      5. Wait for final reboot signal

    When *node_file* is supplied (open file handle), all raw channel I/O is
    written there instead of stdout. *status_cb* (callable(str)) is used for
    brief status messages instead of print(); defaults to print() when None.

    Returns True on success.
    """
    # Decide I/O routing: parallel (node_file) vs sequential (stdout).
    nf = node_file
    _status = status_cb if callable(status_cb) else print

    def _recv(look_for_list, timeout=30):
        if nf:
            return _par_recv_until(channel, nf, look_for_list, timeout)
        return direct_read_until_any(channel, look_for_list, timeout)

    def _send_raw(cmd):
        if nf:
            _par_send(channel, nf, cmd)
        else:
            if log:
                log.log_sent(cmd)
            channel.send(cmd + "\r")

    # ── 1. ifconfig ────────────────────────────────────────────────────────
    if static_ifconfig:
        _iface = static_ifconfig.get("port") or "e0M"
        _addr  = static_ifconfig.get("ip") or ""
        _mask  = static_ifconfig.get("netmask") or ""
        _gw    = static_ifconfig.get("gateway") or ""
        _ifc_cmd = f"ifconfig {_iface} -addr={_addr} -mask={_mask} -gw={_gw}"
        _status(f"\n  [{node_label}] Static LOADER ifconfig: {_iface}  addr={_addr}  mask={_mask}  gw={_gw}")
        if log:
            log.log(f"[{node_label}] static ifconfig: priv set diag; {_ifc_cmd}")
        # Switch to diag privilege level so ifconfig accepts -addr/-mask/-gw flags.
        if nf:
            _par_send(channel, nf, "priv set diag")
            _par_recv_until(channel, nf, ["LOADER", "loader"], timeout=15)
            _par_send(channel, nf, _ifc_cmd)
            output, _ = _par_recv_until(channel, nf, ["LOADER", "loader"], timeout=60)
        else:
            direct_send_and_wait(channel, "priv set diag", "LOADER", timeout=15)
            output = direct_send_and_wait(channel, _ifc_cmd, "LOADER", timeout=60)
        if "loader" not in output.lower():
            _status(f"  ⚠️  [{node_label}] LOADER prompt not seen after static ifconfig; continuing...")
    else:
        _status(f"\n  [{node_label}] Running ifconfig e0M -auto...")
        if log:
            log.log(f"[{node_label}] ifconfig e0M -auto")
        if nf:
            _par_send(channel, nf, "ifconfig e0M -auto")
            output, _ = _par_recv_until(channel, nf, ["LOADER", "loader"], timeout=60)
        else:
            output = direct_send_and_wait(channel, "ifconfig e0M -auto", "LOADER", timeout=60)
        if "loader" not in output.lower():
            _status(f"  ⚠️  [{node_label}] LOADER prompt not seen after ifconfig; continuing...")

    # ── 2+3. netboot + wait for boot menu (retry on download failure) ────────
    _NETBOOT_MAX_ATTEMPTS = 3
    menu_sigs = ["selection (1-", "(1-9)?", "(1-11)?", "(1-12)?"]
    sig_lower = [s.lower() for s in menu_sigs]
    # Patterns that mean the download itself failed — no point waiting 900s
    # for a boot menu that will never arrive.
    _netboot_fail_sigs = [
        "download failed",          # "Download failed: Socket is not connected"
        "socket is not connected",  # explicit variant
        "error opening archive",    # follows download failure
        "no program name specified",# LOADER line after a failed netboot
    ]
    menu_detected = False
    netboot_failed = False
    netboot_fail_reason = ""

    # Discard the contents of NVRAM on the next boot so the netboot install
    # starts from a clean slate (required for 4b reinit; safe to set as it
    # only affects the next boot).
    _status(f"\n  [{node_label}] Setting LOADER env: setenv nvram_discard 1")
    if log:
        log.log(f"[{node_label}] setenv nvram_discard 1")
    if nf:
        _par_send(channel, nf, "setenv nvram_discard 1")
        _par_recv_until(channel, nf, ["LOADER", "loader"], timeout=15)
    else:
        direct_send_and_wait(channel, "setenv nvram_discard 1", "LOADER", timeout=15)

    for _nb_attempt in range(1, _NETBOOT_MAX_ATTEMPTS + 1):
        if _nb_attempt == 1:
            _status(f"\n  [{node_label}] Starting netboot: {pkg_url}")
            if log:
                log.log(f"[{node_label}] netboot {pkg_url}")
        else:
            _status(
                f"\n  [{node_label}] Retrying netboot "
                f"(attempt {_nb_attempt}/{_NETBOOT_MAX_ATTEMPTS}): {pkg_url}"
            )
            if log:
                log.log(
                    f"[{node_label}] netboot retry "
                    f"{_nb_attempt}/{_NETBOOT_MAX_ATTEMPTS}: {pkg_url}"
                )
        _send_raw(f"netboot {pkg_url}")

        # Wait for boot menu ────────────────────────────────────────────────
        _status(f"  [{node_label}] Waiting for boot menu (up to {boot_menu_timeout}s)...")
        buf = ""
        buf_lower = ""
        start = time.monotonic()
        menu_detected = False
        netboot_failed = False
        netboot_fail_reason = ""
        while time.monotonic() - start < boot_menu_timeout:
            if _shutdown_event.is_set():
                return False
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                buf += chunk
                buf_lower += chunk.lower()
                if nf:
                    _par_write(nf, chunk)
                else:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                for sig in sig_lower:
                    if sig in buf_lower:
                        menu_detected = True
                        buf_lower = ""
                        break
                if menu_detected:
                    break
                # Bail early if the bootloader reported a download error.
                for fsig in _netboot_fail_sigs:
                    if fsig in buf_lower:
                        netboot_failed = True
                        netboot_fail_reason = fsig
                        break
                if netboot_failed:
                    break
                if len(buf_lower) > 16384:
                    buf_lower = buf_lower[-8192:]
            time.sleep(0.1)

        if menu_detected:
            break  # success — exit retry loop
        if netboot_failed:
            if _nb_attempt < _NETBOOT_MAX_ATTEMPTS:
                _status(
                    f"\n  ⚠️  [{node_label}] Netboot download failed "
                    f"('{netboot_fail_reason}'); retrying in 10s..."
                )
                if log:
                    log.log(
                        f"[{node_label}] netboot download failed "
                        f"(trigger: '{netboot_fail_reason}'); retrying",
                        prefix="WARN",
                    )
                time.sleep(10)
                # Drain the channel and wait for a clean LOADER prompt
                # before re-issuing netboot. Without this, the retry
                # command arrives while the LOADER is still emitting its
                # error/prompt text, causing the LOADER to see a garbled
                # command line and report 'no program name specified' or
                # 'error opening archive' on attempt 2+.
                _recv(["LOADER", "loader"], timeout=30)
            # else: fall through; post-loop block reports the final error
        else:
            break  # timeout with no menu and no download error — don't retry

    if netboot_failed:
        _status(
            f"  ❌ [{node_label}] Netboot download failed "
            f"('{netboot_fail_reason}' seen in bootloader output). "
            f"Check that the HTTP server IP is reachable from the node's "
            f"management interface and that the package file exists."
        )
        if log:
            log.log(
                f"[{node_label}] netboot aborted — download failure "
                f"(trigger: '{netboot_fail_reason}')",
                prefix="ERROR",
            )
        return False

    if not menu_detected:
        _status(f"  ❌ [{node_label}] Boot menu not detected after netboot.")
        if log:
            log.log(f"[{node_label}] boot menu not detected after netboot", prefix="ERROR")
        return False

    time.sleep(1)  # let the selection prompt fully render
    _status(f"\n  [{node_label}] Boot menu detected – selecting option 7...")
    if log:
        log.log(f"[{node_label}] boot menu detected – sending option 7")
    _send_raw("7")
    time.sleep(2)

    # ── 4. Answer install prompts ──────────────────────────────────────────
    # Prompt 1: "Do you want to continue? {y|n}"
    out, m = _recv(
        ["do you want to continue", "url for the package", "selection (1-"],
        timeout=120,
    )
    if m and "selection (1-" in m.lower():
        # menu re-appeared — option 7 wasn't registered; retry once
        _status(f"  ↻ [{node_label}] Resending option 7...")
        if log:
            log.log(f"[{node_label}] resending boot menu option 7")
        _send_raw("7")
        out, m = _recv(
            ["do you want to continue", "url for the package"],
            timeout=120,
        )
    if m and "do you want to continue" in m.lower():
        _status(f"  [{node_label}] Answering 'do you want to continue?' → y")
        _send_raw("y")
    elif m and "url for the package" in m.lower():
        pass  # ONTAP skipped the first question; fall through
    else:
        _status(f"  ⚠️  [{node_label}] Did not see continuation prompt; continuing anyway...")

    # Prompt 2: "What is the URL for the package?"
    out, m = _recv(
        ["url for the package", "user name", "restore the backup"],
        timeout=120,
    )
    if m and "url for the package" in m.lower():
        _status(f"  [{node_label}] Entering package URL: {pkg_url}")
        if log:
            log.log(f"[{node_label}] sending package URL")
        _send_raw(pkg_url)

    # Mutable container so _do_reboot (defined here) can stop a timer
    # that may be created later in the user-name-prompt branch.
    _progress_stop = [None]  # type: list[threading.Event | None]

    def _do_reboot():
        """Send 'y' to the active reboot prompt and return True.
        The node will reboot; the caller (or a higher-level phase) is
        responsible for waiting on the subsequent boot menu.
        """
        if _progress_stop[0] is not None:
            _progress_stop[0].set()
        _send_raw("y")
        _status(f"  ✅ [{node_label}] Install complete – node is rebooting.")
        if log:
            log.log(f"[{node_label}] reboot triggered; install complete")
        return True

    # Prompt 3: "What is the user name on 'x.x.x.x', if any?"
    out, m = _recv(
        ["user name", "restore the backup", "reboot now", "do you want to reboot"],
        timeout=300,
    )
    if m and "user name" in m.lower():
        _status(f"  [{node_label}] User name prompt → (blank)")
        if nf and hasattr(nf, "name"):
            _status(f"  [{node_label}] 📝 Installing — log: {nf.name}")
        _send_raw("")
        # Start a periodic progress reporter so the terminal doesn't look hung.
        _ps = threading.Event()
        _progress_stop[0] = _ps
        _install_start = time.monotonic()
        def _progress_reporter(_ev=_ps, _t0=_install_start):
            while not _ev.wait(90):
                elapsed = time.monotonic() - _t0
                _status(f"  [{node_label}] ⏳ Node installing ({elapsed:.0f}s elapsed)")
        threading.Thread(target=_progress_reporter, daemon=True).start()
        # Fall through to prompt 4.
    elif m and ("reboot now" in m.lower() or "do you want to reboot" in m.lower()):
        # ONTAP skipped both username and backup prompts.
        _status(f"  [{node_label}] Reboot prompt (early, username+backup skipped) → y")
        return _do_reboot()
    elif m and "restore the backup" in m.lower():
        # ONTAP skipped the username prompt; handle backup restore inline.
        _status(f"  [{node_label}] Restore backup prompt (username skipped) → n")
        if log:
            log.log(f"[{node_label}] restore backup (username skipped) → n")
        _send_raw("n")
        # Skip prompt 4's _recv so we don't double-consume.
        out, m = _recv(["reboot now", "do you want to reboot"], timeout=180)
        if m and ("reboot now" in m.lower() or "do you want to reboot" in m.lower()):
            return _do_reboot()
        _status(f"  ⚠️  [{node_label}] Reboot prompt not seen; node may reboot automatically.")
        _status(f"  ✅ [{node_label}] Install complete.")
        if log:
            log.log(f"[{node_label}] install complete (reboot prompt not seen)")
        return True

    # Prompt 4: "Do you want to restore the backup configuration now? {y|n}"
    out, m = _recv(
        ["restore the backup", "reboot now", "do you want to reboot"],
        timeout=600,
    )
    if m and "restore the backup" in m.lower():
        _status(f"  [{node_label}] Restore backup prompt → n")
        if log:
            log.log(f"[{node_label}] restore backup → n")
        _send_raw("n")
        # Fall through so prompt 5 can catch the reboot prompt that follows.

    elif m and ("reboot now" in m.lower() or "do you want to reboot" in m.lower()):
        # ONTAP skipped the backup-restore step and jumped straight to reboot.
        _status(f"  [{node_label}] Reboot prompt (early, backup skipped) → y")
        return _do_reboot()

    # Prompt 5: "Do you want to reboot now? {y|n}"
    out, m = _recv(["reboot now", "do you want to reboot"], timeout=180)
    if m and ("reboot now" in m.lower() or "do you want to reboot" in m.lower()):
        return _do_reboot()
    if _progress_stop[0] is not None:
        _progress_stop[0].set()
    _status(f"  ⚠️  [{node_label}] Reboot prompt not seen; node may reboot automatically.")
    _status(f"  ✅ [{node_label}] Install complete.")
    if log:
        log.log(f"[{node_label}] install complete (reboot prompt not seen)")
    return True


def _peer_reinit_worker(ip, ctx):
    """Module-level worker for the mode-4b + mode-3 parallel peer auto-join.

    ``ctx`` is a dict produced by ``_run_4b_standalone`` containing the
    per-run state that each thread needs:
      loader_channels, loader_clients, bmc_user, bmc_passwords,
      log, peer_errors, peer_lock, menu_sigs_lower, status.
    """
    loader_channels  = ctx["loader_channels"]
    loader_clients   = ctx["loader_clients"]
    bmc_user         = ctx["bmc_user"]
    bmc_passwords    = ctx["bmc_passwords"]
    log              = ctx["log"]
    peer_errors      = ctx["peer_errors"]
    peer_lock        = ctx["peer_lock"]
    menu_sigs_lower  = ctx["menu_sigs_lower"]
    _status          = ctx["status"]
    _node_reinit_logs = ctx.get("node_reinit_logs") or {}

    peer_ch = loader_channels.get(ip)
    peer_cl = loader_clients.get(ip)
    if peer_ch is None:
        with peer_lock:
            peer_errors.append((ip, "no channel"))
        return
    # Reuse the unified log already opened for this peer node.
    _pnf = _node_reinit_logs.get(ip)

    # Redirect stdout for this thread to the per-node log writer so
    # all wizard/auto_complete_join output goes to the file and only
    # milestone lines (✅/⚠️ etc.) reach the terminal.
    _peer_nlw = _NodeLogWriter(_pnf, interactive=False) if _pnf else None
    _prev_stdout = sys.stdout
    if _peer_nlw:
        sys.stdout = _peer_nlw

    try:
        # Wait for boot menu, then send option 4.
        _status(f"  ⏳ [{ip}] Waiting for boot menu (peer)...")
        _buf_lower = ""
        _start = time.monotonic()
        _found = False
        while time.monotonic() - _start < 900:
            if _shutdown_event.is_set():
                break
            if peer_ch.recv_ready():
                chunk = peer_ch.recv(4096).decode("utf-8", errors="replace")
                _buf_lower += chunk.lower()
                if _pnf:
                    _par_write(_pnf, chunk)
                for sig in menu_sigs_lower:
                    if sig in _buf_lower:
                        _found = True
                        break
                if _found:
                    break
                if len(_buf_lower) > 16384:
                    _buf_lower = _buf_lower[-8192:]
            time.sleep(0.1)
        if _found:
            time.sleep(1)
            _status(f"  ✅ [{ip}] Boot menu detected – selecting option 4...")
            if log:
                log.log(f"[{ip}] boot menu detected – sending option 4")
            if _pnf:
                _par_write(_pnf, "\n>>> sending option 4\n")
            peer_ch.send("4\r")
            time.sleep(2)
            auto_complete_join(
                peer_ch, peer_cl, ip,
                bmc_user, bmc_passwords.get(ip, ""),
                bmc_host=ip,
                no_add_another=True,
            )
        else:
            _status(f"  ⚠️  [{ip}] Boot menu not detected for peer.")
            with peer_lock:
                peer_errors.append((ip, "boot menu timeout"))
    finally:
        # Restore stdout; log file is closed after all workers finish.
        sys.stdout = _prev_stdout


def _run_4b_standalone(log, resuming: bool = False):
    """Full standalone 4b workflow: collect BMCs, reset to LOADER,
    netboot-install ONTAP, verify version.  If *resuming* is True the
    module-level ``_checkpoint`` is used to skip already-completed phases.
    Returns True on success.
    """
    global _peer_log_paths, _checkpoint
    _peer_log_paths = {}  # reset for this run

    # ── Shared state ───────────────────────────────────────────────────────
    # Serializes the brief status lines printed to the console by parallel
    # worker threads, so they never interleave mid-line.
    _stdout_lock = threading.Lock()

    def _status(msg):
        with _stdout_lock:
            print(msg)
        # Mirror to the session log so the log file is a complete record.
        if log:
            stripped = msg.strip()
            if "❌" in stripped:
                prefix = "ERROR"
            elif "⚠️" in stripped:
                prefix = "WARN"
            else:
                prefix = "INFO"
            log.log(stripped, prefix=prefix)

    # Determine log directory from the session logger when available.
    if log and hasattr(log, "log_file"):
        _log_dir = os.path.dirname(log.log_file)
    else:
        try:
            _log_base = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            _log_base = os.getcwd()
        _log_dir = os.path.join(_log_base, "logs", datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(_log_dir, exist_ok=True)

    # Per-node file handles – opened before workers start, closed at the end.
    _node_files = {}   # {ip: file}

    # ── Step 1: Collect BMC info ───────────────────────────────────────────
    if resuming and _checkpoint and _checkpoint.bmc_ips:
        # Re-use the saved BMC IP list; ask only for passwords (never stored).
        bmc_ips = _checkpoint.bmc_ips
        print(f"\n  🔖 Resuming with {len(bmc_ips)} BMC(s) from checkpoint: "
              f"{', '.join(bmc_ips)}")
        print("  (Passwords are not stored in the checkpoint — please re-enter.)")
        _same = input(
            "  Use the same password for all BMCs? [Y/n]: "
        ).strip().lower()
        bmc_user = input("  BMC username [admin]: ").strip() or "admin"
        if _same != "n":
            _shared_pw = getpass.getpass("  BMC password: ")
            bmc_passwords = {ip: _shared_pw for ip in bmc_ips}
        else:
            bmc_passwords = {}
            for _ip in bmc_ips:
                bmc_passwords[_ip] = getpass.getpass(f"  Password for {_ip}: ")
        if log:
            log.log(f"4b resume: using {len(bmc_ips)} BMC(s) from checkpoint: {bmc_ips}")
    else:
        bmc_ips, bmc_user, bmc_passwords = _collect_netboot_bmcs()
        if bmc_ips is None:
            print("\n  Aborting 4b.")
            return False
        if log:
            log.log(f"4b: {len(bmc_ips)} node(s): {bmc_ips}")

    # If any BMC password was left empty, ask for the cluster admin password
    # now so it is available when the cluster setup wizard runs later.
    if any(not bmc_passwords.get(ip) for ip in bmc_ips):
        global _cluster_config
        if not (_cluster_config and _cluster_config.get("admin_password")):
            print("")
            _pre_cluster_pw = getpass.getpass(
                "  Enter the cluster admin password to be used during "
                "cluster configuration: "
            )
            if _pre_cluster_pw:
                if not isinstance(_cluster_config, dict):
                    _cluster_config = {}
                _cluster_config["admin_password"] = _pre_cluster_pw
                _ctx_sync_from_globals()
                if log:
                    log.log("4b: cluster admin password pre-collected (BMC password was empty)")

    # ── Step 1b: Package selection (ask now, before operations begin) ──────
    if log:
        log.start_phase("4b – Package Selection")
    print("\n  Select the ONTAP package to netboot-install:")
    src_type, src_value = _find_upgrade_package()
    if src_type is None:
        print("  No package selected. Aborting.")
        if log:
            log.end_phase(outcome="FAIL", note="no package selected")
        return False
    if log:
        log.end_phase()

    # ── Step 1c: Reinit questions (ask now, before operations begin) ───────
    print("\n  ✅ Package selected. Now collecting all setup information upfront.")
    reinit_ans = input(
        "\n  Would you like to reinit the cluster after the ONTAP installation? [y/N]: "
    ).strip().lower()

    _do_reinit = reinit_ans == "y"
    _mode_sel = None

    if _do_reinit:
        print("\n  Select reinit mode:")
        print("    1a. Format first node (interactive)")
        print("    1b. Format first node + cluster setup (automatic)")
        print("     3. End-to-end auto initialize (1b + auto-add all peer nodes)")
        print("")
        while True:
            _mode_sel = _prompt("  Enter 1a, 1b, or 3: ").lower()
            if _mode_sel in ("1a", "1b", "3"):
                break
            print("  ⚠️  Invalid choice.")

        global _operation_mode, _auto_setup, _auto_add
        if _mode_sel == "1a":
            _operation_mode = 1
            _auto_setup = False
            _auto_add = False
        elif _mode_sel == "1b":
            _operation_mode = 1
            _auto_setup = True
            _auto_add = False
        else:  # 3
            _operation_mode = 3
            _auto_setup = True
            _auto_add = True

        if log:
            log.log(f"4b: reinit mode selected: {_mode_sel}")

        # Discover and load a config file.
        print("\n  " + "─" * 58)
        print("  Looking for a reinit config file for the cluster setup...")
        _cfg_loaded = _discover_and_prompt_config(_run_context)
        if _cfg_loaded:
            if log:
                log.log(f"4b: reinit config loaded: {_cfg_loaded}")
        else:
            print("  ℹ️  No config file loaded; wizard will prompt for all values.")
            if log:
                log.log("4b: no config file loaded; wizard will use manual prompts")

        # Collect node-management IP details for every node.
        collect_node_mgmt_per_bmc(bmc_ips[0], bmc_ips[1:])

        # For automatic modes (1b/3), collect the cluster-level setup answers.
        if _auto_setup:
            collect_cluster_config()

        # Ask about passwordless SSH for automatic modes (1b/3) up-front so
        # nothing needs to be asked mid-run.
        if _auto_setup:
            global _setup_passwordless_ssh
            _ssh_q = _prompt(
                "\n  Set up passwordless SSH to cluster management after setup? [y/N]: "
            , "n").lower()
            _setup_passwordless_ssh = (_ssh_q == "y")
            if log:
                log.log(f"4b: passwordless SSH requested: {_setup_passwordless_ssh}")

            # License: collect key(s) or validate the license file path now,
            # before the BMC reinit/install begins, so the operator can fix
            # issues early. Direct mode 1/3 does this in main(); the 4b path
            # dispatches before that block so we ask here instead.
            _collect_license_config(_run_context)
            if log and _license_mode:
                log.log(f"4b: ONTAP license configured (mode={_license_mode})")

        # Physical disk zeroing applies to every reinit sub-mode (1a/1b/3);
        # it is set by the LOADER stage on the primary node. The direct
        # mode 1/3 menu entries prompt for this up-front, so 4b's reinit
        # path must do the same to avoid silently skipping the option.
        global _physical_zeroing
        print("\n  ℹ️   Physical zeroing can help ensure consistency in throughput results.")
        _pz_q = _prompt(
            "  Do you want to physically zero all disks? (This can add time to the reinit process) [y/N]: "
        , "n").lower()
        _physical_zeroing = (_pz_q == "y")
        if _physical_zeroing:
            print("  ℹ️   Physical disk zeroing enabled (raid.use-physical-zeroing).")
        if log:
            log.log(f"4b: physical disk zeroing requested: {_physical_zeroing}")

    # ── Static vs DHCP ifconfig in LOADER ─────────────────────────────────
    global _netboot_static_ip
    _sip_ans = _prompt(
        "\n  Use static IP in LOADER instead of DHCP (ifconfig -auto)? [y/N]: "
    , "n").lower()
    _netboot_static_ip = (_sip_ans == "y")
    if _netboot_static_ip:
        # Seed _node_mgmt_by_bmc from the config for any BMC not already
        # populated (e.g. when reinit was not requested so
        # collect_node_mgmt_per_bmc did not run).
        for _bip in bmc_ips:
            if _bip not in _node_mgmt_by_bmc:
                _ncfg = _node_cfg_for(_bip)
                _p = _ncfg.get("node_mgmt_port") or _ncfg.get("port")
                _i = _ncfg.get("node_mgmt_ip")   or _ncfg.get("ip")
                _m = _ncfg.get("node_mgmt_netmask") or _ncfg.get("netmask")
                _g = _ncfg.get("node_mgmt_gateway") or _ncfg.get("gateway")
                if any((_p, _i, _m, _g)):
                    _node_mgmt_by_bmc[_bip] = {
                        "port": _p, "ip": _i, "netmask": _m, "gateway": _g,
                    }
        _missing_static = [
            ip for ip in bmc_ips
            if ip not in _node_mgmt_by_bmc
            or not all(_node_mgmt_by_bmc[ip].get(k) for k in ("ip", "netmask", "gateway"))
        ]
        if _missing_static:
            print(f"  ⚠️  No complete static IP config found for: {', '.join(_missing_static)}")
            print("     These nodes will fall back to 'ifconfig -auto'.")
        if log:
            log.log(f"4b: static LOADER ifconfig enabled for {len(bmc_ips) - len(_missing_static)}/{len(bmc_ips)} node(s)")
    if log:
        log.log(f"4b: static ifconfig in LOADER: {_netboot_static_ip}")

    print("\n  ✅ All setup information collected. Starting operations...")
    if log:
        log.log(f"4b: all upfront questions answered; do_reinit={_do_reinit}, mode={_mode_sel}")

    # ── Initialise / update checkpoint ────────────────────────────────────
    if not resuming or _checkpoint is None:
        _checkpoint = CheckpointManager()
    _checkpoint.init_run(
        mode=f"4b-{_mode_sel or 'no-reinit'}",
        bmc_ips=bmc_ips,
        log_dir=_log_dir,
        config_path=getattr(log, "log_file", "") or "",
    )
    if log:
        log.log(f"4b: checkpoint initialised at {_checkpoint._path}")

    # ── Resume status display ────────────────────────────────────────────
    # The checkpoint records each phase as it completes; on a resumed run
    # we show the operator what was already done before re-running so they
    # know roughly where the prior run left off. (Phase-level skip is a
    # future improvement — the operator can re-confirm progress visually.)
    if resuming and _checkpoint:
        _install_done_ips = _checkpoint.nodes_done_for("install_done")
        _reinit_done_ips  = _checkpoint.nodes_done_for("reinit_loader")
        _opt4_done_ips    = _checkpoint.nodes_done_for("peer_option4_done")
        _joined_ips       = _checkpoint.nodes_done_for("peer_joined")
        print("\n  🔖 Resume status from checkpoint:")
        for _ip in bmc_ips:
            _flags = []
            if _ip in _install_done_ips:
                _flags.append("install_done")
            if _ip in _reinit_done_ips:
                _flags.append("reinit_loader")
            if _ip in _opt4_done_ips:
                _flags.append("peer_option4_done")
            if _ip in _joined_ips:
                _flags.append("peer_joined")
            print(f"     [{_ip}] {', '.join(_flags) if _flags else '(no prior progress)'}")
        if _checkpoint.is_done("primary_bootmenu_done"):
            print("     primary_bootmenu_done : ✅ (primary cleared boot menu)")
        if _checkpoint.is_done("cluster_formed"):
            print("     cluster_formed        : ✅ (prior run completed cluster setup)")
        if _checkpoint.is_done("primary_setup_done"):
            print("     primary_setup_done    : ✅ (primary wizard finished)")
        if _checkpoint.is_done("option3_complete"):
            print("     option3_complete      : ✅")
        if log:
            log.log(
                "4b resume: prior progress — install_done="
                f"{_install_done_ips}, reinit_loader={_reinit_done_ips}, "
                f"peer_option4_done={_opt4_done_ips}, "
                f"peer_joined={_joined_ips}, "
                f"primary_bootmenu_done={_checkpoint.is_done('primary_bootmenu_done')}, "
                f"cluster_formed={_checkpoint.is_done('cluster_formed')}, "
                f"primary_setup_done={_checkpoint.is_done('primary_setup_done')}"
            )

    # Open per-node log files.
    for ip in bmc_ips:
        try:
            _node_files[ip] = _node_log_open(ip, _log_dir, prefix="4b_node")
            print(f"  📝 [{ip}] Log: {_node_files[ip].name}")
        except Exception as exc:
            print(f"  ⚠️  [{ip}] Could not open node log: {exc}")
            _node_files[ip] = None

    # ── Decide whether to skip the install phase (Steps 2–6a) ────────────
    # On a resumed run where every BMC IP already has install_done recorded,
    # the install is complete and we can jump straight to Step 6b (reinit
    # reconnect). install_done is recorded at the end of Step 6a (option 6)
    # only after a node reaches the ONTAP login prompt, so it is the correct
    # signal that the destructive install work is finished.
    _install_done_ips_set = (
        set(_checkpoint.nodes_done_for("install_done")) if _checkpoint else set()
    )
    _skip_install = bool(
        resuming and _install_done_ips_set
        and all(ip in _install_done_ips_set for ip in bmc_ips)
    )
    if _skip_install:
        print(
            f"\n  🔖 Checkpoint: all {len(bmc_ips)} node(s) have install_done "
            "— skipping Steps 2–6a (BMC connect / reset / netboot / option 6)."
        )
        if log:
            log.log("4b resume: skipping install phase — install_done for all nodes")

    # Initialise variables used after the install block so the skip path
    # does not NameError downstream.
    bmc_clients: "dict[str, object]" = {}
    bmc_channels: "dict[str, object]" = {}
    loader_channels: "dict[str, object]" = {}
    loader_clients: "dict[str, object]" = {}
    pkg_url = ""
    httpd = None
    first_ip = bmc_ips[0]

    if not _skip_install:
        # ── Step 2: SSH to all BMCs in parallel, verify BMC prompt ────────────
        print(f"\n  Connecting to {len(bmc_ips)} BMC(s) in parallel...")
        if log:
            log.start_phase("4b – BMC SSH Connections")

        bmc_clients = {}   # {ip: paramiko.SSHClient}
        bmc_channels = {}  # {ip: channel}
        connect_lock = threading.Lock()
        connect_errors = []

        def _connect_worker(ip, stagger_idx=0):
            # Stagger connection attempts by 2s per node index to avoid
            # hammering all BMCs simultaneously — simultaneous SSH handshakes
            # can exhaust the BMC daemon's connection limit and cause banner
            # timeout errors.
            if stagger_idx > 0:
                time.sleep(stagger_idx * 2)
            nf = _node_files.get(ip)
            try:
                _status(f"  ⏳ [{ip}] Connecting to BMC...")
                # Build fallback password list: BMC password first (primary),
                # then the cluster admin password if one was collected.
                _fallbacks = _bmc_fallback_passwords(ip, bmc_passwords)
                client, _u, _p = _ssh_connect_with_retry(
                    ip, bmc_user, bmc_passwords.get(ip, ""),
                    label=f"BMC/{ip}", max_attempts=len(_fallbacks) + 1,
                    interactive=False, fallback_passwords=_fallbacks,
                )
                ch = _open_shell(client)
                # Wait for BMC prompt – output goes to node file only.
                out, matched = _par_recv_until(ch, nf, ["y/n", ">"], timeout=15)
                if matched and "y/n" in matched.lower():
                    if nf:
                        _par_write(nf, "\n>>> y  (taking over existing session)\n")
                    ch.send("y\r")
                    time.sleep(2)
                    _par_recv_until(ch, nf, [">"], timeout=15)
                if not matched and ">" not in (out or ""):
                    raise RuntimeError(f"No BMC prompt from {ip}")
                with connect_lock:
                    bmc_clients[ip] = client
                    bmc_channels[ip] = ch
                _status(f"  ✅ [{ip}] BMC shell ready.")
                if log:
                    log.log(f"[{ip}] BMC shell ready")
            except Exception as exc:
                with connect_lock:
                    connect_errors.append((ip, str(exc)))
                _status(f"  ❌ [{ip}] Connect failed: {exc}")
                if log:
                    log.log(f"[{ip}] BMC connect failed: {exc}", prefix="ERROR")

        _run_parallel(bmc_ips, _connect_worker, with_index=True)

        if connect_errors:
            print(f"\n  ❌ Failed to connect to {len(connect_errors)} BMC(s):")
            for ip, err in connect_errors:
                print(f"     {ip}: {err}")
            if log:
                log.end_phase(outcome="FAIL", note="BMC connect failures")
            return False

        if log:
            log.end_phase()

        # ── Step 3: Simultaneously reset all nodes and reach LOADER ──────────
        print(f"\n  Resetting {len(bmc_ips)} node(s) to LOADER (output → per-node logs)...")
        if log:
            log.start_phase("4b – Reset to LOADER")

        loader_channels = {}   # {ip: channel}  at LOADER prompt
        loader_clients = {}    # {ip: client}
        loader_errors = []

        def _reset_worker(ip):
            nf = _node_files.get(ip)
            ch = bmc_channels[ip]
            cl = bmc_clients[ip]
            try:
                # system reset (auto-confirm y/n prompt)
                _status(f"  ⏳ [{ip}] Sending system reset...")
                if nf:
                    _par_write(nf, "\n>>> system reset\n")
                ch.send("system reset\r")
                out, m = _par_recv_until(ch, nf, ["y/n", ">"], timeout=15)
                if m and "y/n" in m.lower():
                    if nf:
                        _par_write(nf, "\n>>> y  (confirming system reset)\n")
                    ch.send("y\r")
                _status(f"  ⏳ [{ip}] Node rebooting...")
                if log:
                    log.log(f"[{ip}] system reset issued")
                time.sleep(3)
                _par_recv_until(ch, nf, [">"], timeout=20)

                # system console
                if nf:
                    _par_write(nf, "\n>>> system console\n")
                ch.send("system console\r")
                out2, m2 = _par_recv_until(
                    ch, nf,
                    ["y/n", "ctrl-d", "serial console", "loader", "autoboot"],
                    timeout=15,
                )
                if m2 and "y/n" in m2.lower():
                    if nf:
                        _par_write(nf, "\n>>> y  (taking over console session)\n")
                    ch.send("y\r")
                    time.sleep(2)

                # Monitor for AUTOBOOT/LOADER – all raw output → node file only.
                buf = ""
                start = time.monotonic()
                found = False
                while time.monotonic() - start < 600:
                    if _shutdown_event.is_set():
                        break
                    if ch.recv_ready():
                        chunk = ch.recv(4096).decode("utf-8", errors="replace")
                        buf += chunk
                        if nf:
                            _par_write(nf, chunk)
                        if "starting autoboot press ctrl-c to abort" in buf.lower():
                            _status(f"  🛑 [{ip}] Interrupting AUTOBOOT...")
                            if log:
                                log.log(f"[{ip}] AUTOBOOT detected – sending Ctrl+C")
                            if nf:
                                _par_write(nf, "\n>>> ^C (interrupting autoboot)\n")
                            for _ in range(6):
                                ch.send("\x03")
                                time.sleep(0.3)
                            buf = ""
                        elif _LOADER_PROMPT_RE.search(buf):
                            found = True
                            break
                        if len(buf) > 8192:
                            buf = buf[-4096:]
                    time.sleep(0.1)

                if found:
                    with connect_lock:
                        loader_channels[ip] = ch
                        loader_clients[ip] = cl
                    _status(f"  ✅ [{ip}] At LOADER prompt.")
                    if log:
                        log.log(f"[{ip}] reached LOADER prompt")
                else:
                    with connect_lock:
                        loader_errors.append((ip, "LOADER prompt timeout"))
                    _status(f"  ❌ [{ip}] LOADER not reached (timeout).")
                    if log:
                        log.log(f"[{ip}] LOADER not reached (timeout)", prefix="ERROR")
            except Exception as exc:
                with connect_lock:
                    loader_errors.append((ip, str(exc)))
                _status(f"  ❌ [{ip}] Error during reset: {exc}")
                if log:
                    log.log(f"[{ip}] reset error: {exc}", prefix="ERROR")

        _run_parallel(bmc_ips, _reset_worker)

        if loader_errors:
            print(f"\n  ❌ {len(loader_errors)} node(s) did not reach LOADER:")
            for ip, err in loader_errors:
                print(f"     {ip}: {err}")
            if log:
                log.end_phase(outcome="FAIL", note="LOADER not reached on all nodes")
            return False

        if log:
            log.end_phase()

        # ── Step 4: Start HTTP server if serving a local file ─────────────────
        if log:
            log.start_phase("4b – HTTP Server")
        httpd = None
        if src_type == "file":
            _ht, pkg_url, httpd = _start_http_server(src_value)
            print(f"  🌐 HTTP server started: {pkg_url}")
            if log:
                log.log(f"HTTP server URL: {pkg_url}")
        else:
            pkg_url = src_value
            print(f"  🌐 Using URL: {pkg_url}")
        if log:
            log.end_phase()

        # ── Step 5: ifconfig + netboot on all nodes in parallel ────────────────
        print(f"\n  Starting netboot-install on {len(bmc_ips)} node(s) in parallel"
              f" (output → per-node logs)...")
        if log:
            log.start_phase("4b – Netboot Install")

        install_results = {}  # {ip: True/False}

        def _install_worker(ip):
            nf = _node_files.get(ip)
            ch = loader_channels[ip]
            _static = _node_mgmt_by_bmc.get(ip) if _netboot_static_ip else None
            def _scb(msg):
                _status(msg)
            ok = _run_netboot_install_sequence(
                ch, pkg_url, node_label=ip, log=log,
                boot_menu_timeout=900, node_file=nf, status_cb=_scb,
                static_ifconfig=_static,
            )
            with connect_lock:
                install_results[ip] = ok
            # Note: install_done checkpoint is marked AFTER option 6 completes
            # (Step 6a, below) so it reflects the node having reached ONTAP
            # login — not just the netboot transfer.

        threads = [threading.Thread(target=_install_worker, args=(ip,), daemon=True)
                   for ip in bmc_ips]
        for t in threads:
            t.start()

        # All install threads are now running in parallel.  Print a single
        # status block so the operator knows where to follow progress.
        _real_stdout.write("\n  ⏳ Nodes installing — check logs for details:\n")
        for _ip in bmc_ips:
            _nf = _node_files.get(_ip)
            _log_path = _nf.name if _nf else "(no log file)"
            _real_stdout.write(f"    [{_ip}] {_log_path}\n")
        _real_stdout.flush()

        for t in threads:
            t.join()

        if httpd:
            try:
                httpd.shutdown()
            except Exception:
                pass

        failed = [ip for ip, ok in install_results.items() if not ok]
        if failed:
            print(f"\n  ❌ Netboot install failed on: {', '.join(failed)}")
            if log:
                log.end_phase(outcome="FAIL", note=f"install failed: {failed}")
            return False

        if log:
            log.end_phase()

        # ── Step 6: Transition to reinit ──────────────────────────────────────
        first_ip = bmc_ips[0]

        if log:
            log.log(f"4b: netboot install complete on {len(bmc_ips)} node(s)")

        print(f"\n  ✅ Netboot complete on all {len(bmc_ips)} node(s).")
        # (_do_reinit and _mode_sel were collected upfront before operations began)

        # Close and log all node files now (before option 6 and init take over).
        for ip, nf in list(_node_files.items()):
            if nf:
                try:
                    nf.close()
                    print(f"  📝 [{ip}] Log saved: {nf.name}")
                except Exception:
                    pass
        _node_files.clear()

        # ── Step 6a: Option 6 on all nodes (finish the netboot/install) ───────
        # Always run option 6 first: this updates flash from backup config and
        # boots every node to the login prompt, confirming the install succeeded.
        # If reinit was requested we reconnect via BMC afterwards (Step 6b).
        print("\n  Selecting boot menu option 6 (Update flash from backup config) on all nodes"
              " to complete the netboot/install...")
        if log:
            log.log(f"4b: running option 6 on all nodes to finish install (do_reinit={_do_reinit})")

        _menu_sigs_lower = [
                "selection (1-", "(1-9)?", "(1-11)?", "(1-12)?",
                # Shown after netboot install when boot device has changed:
                "use option (6) to restore the system configuration",
                "normal boot is prohibited",
            ]

        # IPs that reached ONTAP login: without needing a boot-menu / option 6.
        # Populated by _select_option6; checked after all workers join.
        _opt6_login_nodes = set()

        def _mark_install_done(ip):
            """Persist install_done for *ip* immediately so a kill mid-run
            does not lose per-node progress. Safe to call concurrently:
            CheckpointManager._save atomically replaces the JSON file."""
            if _checkpoint is None:
                return
            try:
                with connect_lock:
                    _checkpoint.mark_node_done("install_done", ip)
                if log:
                    log.log(f"[{ip}] checkpoint: install_done saved")
            except Exception:
                pass

        def _select_option6(ip, ch):
            if ch is None:
                _status(f"  ⚠️  [{ip}] No channel – skipping option 6.")
                return
            # Open a per-node log for all raw boot output.
            _nf6 = None
            try:
                _nf6 = _node_log_open(ip, _log_dir, prefix="4b_opt6")
                _status(f"  📝 [{ip}] Boot output → {_nf6.name}")
            except Exception as _e:
                _status(f"  ⚠️  [{ip}] Could not open log file: {_e}")

            def _drain(timeout_s, stop_sigs):
                """Read from channel for up to timeout_s seconds.
                Returns the first matched signal string, or None on timeout."""
                _buf = ""
                _t = time.monotonic()
                while time.monotonic() - _t < timeout_s:
                    if _shutdown_event.is_set():
                        return None
                    if ch.recv_ready():
                        _chunk = ch.recv(4096).decode("utf-8", errors="replace")
                        _buf += _chunk
                        if _nf6:
                            _par_write(_nf6, _chunk)
                        for _s in stop_sigs:
                            if _s in _buf.lower():
                                return _s
                        if len(_buf) > 16384:
                            _buf = _buf[-8192:]
                    time.sleep(0.1)
                return None

            # ── VLDB online timeout detection ──────────────────────────────────
            # When VLDB fails to come online after option 6 the script used to
            # prompt the operator (y/n) to proceed to reinit. That made the
            # 4b run block indefinitely on stdin in --bg / screen sessions
            # and almost always got "y" anyway, so we now auto-answer "y"
            # and log it.
            def _vldb_prompt():
                with _stdout_lock:
                    _real_stdout.write(
                        f"\n  ⚠️  [{ip}] Warning: Timed out waiting for VLDB online detected.\n"
                        f"  Cluster node not coming online after option 6. "
                        f"Auto-proceeding to reinitialization process (y).\n"
                    )
                    _real_stdout.flush()
                if log:
                    log.log(
                        f"[{ip}] VLDB timeout: auto-answered 'y' "
                        "(proceed to reinit)",
                        prefix="WARN",
                    )
                return True

            # ── Phase 1: wait for any boot-menu indicator ──────────────────
            # The warning text ("Normal Boot is prohibited") appears a few
            # seconds before the numbered selection prompt renders. We detect
            # either, then explicitly drain until the selection prompt is ready
            # before sending "6".
            _early_sigs = [
                "use option (6) to restore the system configuration",
                "normal boot is prohibited",
            ]
            _sel_sigs = ["selection (1-", "(1-9)?", "(1-11)?", "(1-12)?"]
            _all_sigs = _early_sigs + _sel_sigs + ["login:"]

            # ── BMC reconnect helper ────────────────────────────────────────
            # If the BMC SSH session or system-console session has dropped, this
            # re-establishes the connection and re-enters system console so the
            # boot-menu wait can continue on the same `ch` binding.
            def _reenter_console():
                nonlocal ch
                _status(f"  ⚠️  [{ip}] BMC session dropped – reconnecting and re-entering system console...")
                if log:
                    log.log(f"[{ip}] BMC session dropped; reconnecting", prefix="WARN")
                try:
                    _old_cl = loader_clients.get(ip)
                    if _old_cl:
                        try:
                            _old_cl.close()
                        except Exception:
                            pass
                    _pw = bmc_passwords.get(ip, "")
                    _new_cl = paramiko.SSHClient()
                    _new_cl.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    _new_cl.connect(ip, username=bmc_user, password=_pw,
                                    timeout=20, allow_agent=False, look_for_keys=False)
                    _new_ch = _open_shell(_new_cl)
                    # Drain the BMC banner, then enter system console.
                    time.sleep(1)
                    _nb = ""
                    _nb_end = time.monotonic() + 5
                    while time.monotonic() < _nb_end:
                        if _new_ch.recv_ready():
                            _nb += _new_ch.recv(4096).decode("utf-8", errors="replace")
                        time.sleep(0.1)
                    if _nf6:
                        _par_write(_nf6, "\n>>> [reconnect] system console\n")
                    _new_ch.send("system console\r")
                    _rc_buf = ""
                    _rc_end = time.monotonic() + 15
                    while time.monotonic() < _rc_end:
                        if _new_ch.recv_ready():
                            _rc_c = _new_ch.recv(4096).decode("utf-8", errors="replace")
                            _rc_buf += _rc_c
                            if _nf6:
                                _par_write(_nf6, _rc_c)
                            _rc_l = _rc_buf.lower()
                            if "y/n" in _rc_l:
                                _new_ch.send("y\r")
                                if _nf6:
                                    _par_write(_nf6, "\n>>> y (takeover)\n")
                                time.sleep(0.5)
                            elif any(s in _rc_l for s in (
                                    "ctrl-d", "type exit", "selection", "login:", "loader", "autoboot")):
                                break
                        time.sleep(0.1)
                    ch = _new_ch
                    loader_channels[ip] = _new_ch
                    loader_clients[ip]  = _new_cl
                    _status(f"  ✅ [{ip}] BMC reconnected – system console active.")
                    if log:
                        log.log(f"[{ip}] BMC reconnected; system console active")
                except Exception as _exc:
                    _status(f"  ❌ [{ip}] BMC reconnect failed: {_exc}")
                    if log:
                        log.log(f"[{ip}] BMC reconnect failed: {_exc}", prefix="ERROR")

            buf_lower = ""
            start = time.monotonic()
            found = False
            _matched_sig = None
            _boot_menu_timeout = 1800
            _next_progress = start + 60
            while time.monotonic() - start < _boot_menu_timeout:
                if _shutdown_event.is_set():
                    break
                now = time.monotonic()
                if now >= _next_progress:
                    elapsed = int(now - start)
                    _status(f"  ⏳ [{ip}] Still waiting for boot menu... ({elapsed}s elapsed)")
                    _next_progress = now + 60
                    # ── Heartbeat: keep the channel alive ──────────────────
                    # If the channel has closed entirely, reconnect the BMC SSH
                    # session and re-enter system console.
                    if ch.closed:
                        _reenter_console()
                    else:
                        # Send a carriage return so the node re-displays its
                        # current prompt (login: or boot menu) if it has already
                        # reached that state but gone quiet.
                        try:
                            ch.send("\r")
                        except OSError:
                            _reenter_console()
                if ch.recv_ready():
                    chunk = ch.recv(4096).decode("utf-8", errors="replace")
                    buf_lower += chunk.lower()
                    if _nf6:
                        _par_write(_nf6, chunk)
                    # ── BMC-prompt detection ────────────────────────────────
                    # If system console dropped we receive the BMC shell prompt.
                    # Detect via the module-level signature ("bmc>") and by
                    # noticing any short line that ends with "> " and contains
                    # "bmc" (covers hostnames like "node-bmc-01> ").
                    _chunk_l = chunk.lower()
                    _looks_like_bmc = (
                        _BMC_PROMPT_SIG in _chunk_l
                        or ("bmc" in _chunk_l and _chunk_l.rstrip().endswith(">"))
                    )
                    if _looks_like_bmc and not any(s in _chunk_l for s in _all_sigs):
                        _reenter_console()
                        buf_lower = ""
                        continue
                    for sig in _all_sigs:
                        if sig in buf_lower:
                            found = True
                            _matched_sig = sig
                            break
                    if found:
                        break
                    if len(buf_lower) > 16384:
                        buf_lower = buf_lower[-8192:]
                time.sleep(0.1)

            if not found:
                _status(f"  ⚠️  [{ip}] Boot menu not detected; skipping option 6.")
                if log:
                    log.log(f"[{ip}] boot menu not seen for option 6 (timeout)", prefix="WARN")
                if _nf6:
                    with suppress(Exception):
                        _nf6.close()
                return

            # ── Node already running ONTAP (login: before boot menu) ────────
            if _matched_sig == "login:":
                _status(f"  [{ip}] Node is already at ONTAP login prompt – no option 6 needed.")
                if log:
                    log.log(f"[{ip}] reached login: without boot menu; skipping option 6")
                with connect_lock:
                    _opt6_login_nodes.add(ip)
                _mark_install_done(ip)
                if _nf6:
                    with suppress(Exception):
                        _nf6.close()
                return

            # ── Phase 2: if we matched an early-warning sig, keep draining
            # until the numbered selection prompt actually appears (up to 60s).
            matched_early = any(sig in buf_lower for sig in _early_sigs)
            sel_already_seen = any(sig in buf_lower for sig in _sel_sigs)
            if matched_early and not sel_already_seen:
                _status(f"  [{ip}] Boot warning detected – waiting for selection prompt...")
                if log:
                    log.log(f"[{ip}] boot warning seen; waiting for selection prompt")
                _drain(60, _sel_sigs)

            # Short pause to let the selection prompt fully render.
            time.sleep(2)

            _status(f"  [{ip}] Boot menu ready – sending option 6...")
            if log:
                log.log(f"[{ip}] boot menu ready – sending option 6")
            if _nf6:
                _par_write(_nf6, "\n>>> sending option 6\n")
            try:
                ch.send("6\r")
            except OSError as exc:
                _status(f"  ❌ [{ip}] Channel closed before option 6 could be sent: {exc}")
                if log:
                    log.log(f"[{ip}] channel closed before option 6: {exc}", prefix="ERROR")
                if _nf6:
                    with suppress(Exception):
                        _nf6.close()
                return

            # ── Phase 3: wait for confirmation prompt ──────────────────────
            # Prompt: "Are you sure you want to continue?:"
            # If the menu re-appears it means our "6" wasn't registered; retry.
            _m = _drain(30, ["are you sure you want to continue", "selection (1-"])
            if _m and "are you sure you want to continue" in _m:
                _status(f"  [{ip}] Option 6 confirmation → y")
                if log:
                    log.log(f"[{ip}] option 6 confirmation → y")
                if _nf6:
                    _par_write(_nf6, "\n>>> y (option 6 confirmation)\n")
                try:
                    ch.send("y\r")
                except OSError:
                    pass
            elif _m and "selection (1-" in _m:
                # Menu reappeared – retry once.
                _status(f"  ↻ [{ip}] Resending option 6 (menu reappeared)...")
                if log:
                    log.log(f"[{ip}] resending boot menu option 6")
                if _nf6:
                    _par_write(_nf6, "\n>>> resending option 6\n")
                time.sleep(1)
                try:
                    ch.send("6\r")
                except OSError:
                    if _nf6:
                        with suppress(Exception):
                            _nf6.close()
                    return
                _m2 = _drain(30, ["are you sure you want to continue"])
                if _m2:
                    _status(f"  [{ip}] Option 6 confirmation → y")
                    if log:
                        log.log(f"[{ip}] option 6 confirmation (retry) → y")
                    if _nf6:
                        _par_write(_nf6, "\n>>> y (option 6 confirmation retry)\n")
                    try:
                        ch.send("y\r")
                    except OSError:
                        pass
                else:
                    _status(f"  ⚠️  [{ip}] Option 6 confirmation not seen after retry.")
                    if log:
                        log.log(f"[{ip}] option 6 confirmation not seen (retry timeout)", prefix="WARN")
                    if _nf6:
                        with suppress(Exception):
                            _nf6.close()
                    return
            else:
                _status(f"  ⚠️  [{ip}] Option 6 confirmation not seen.")
                if log:
                    log.log(f"[{ip}] option 6 confirmation not seen (timeout)", prefix="WARN")
                if _nf6:
                    with suppress(Exception):
                        _nf6.close()
                return

            # ── Phase 4: wait for login: prompt OR second boot menu ────────
            # After option 6 there is sometimes a false-positive login: prompt
            # that appears briefly before the node reboots a second time.
            # If "The boot device has changed / Normal Boot is prohibited"
            # appears, the node can't boot normally – send option 4 (Initialize).
            _reboot_indicators = [
                "starting autoboot", "press ctrl-c", "loader>",
                "autoboot in", "selection (1-", "normal boot is prohibited",
            ]
            # Each node waits up to 30 min after option 6 for the boot to complete.
            _boot_timeout = 1800
            _boot_timeout_min = _boot_timeout // 60
            _status(f"  ⏳ [{ip}] Option 6 confirmed – waiting for node to boot (up to {_boot_timeout_min} min)...")
            if log:
                log.log(f"[{ip}] option 6 confirmed; waiting for login prompt (up to {_boot_timeout_min} min)")

            _boot_menu_sigs_6 = [
                "normal boot is prohibited",
                "boot device has changed",
                "selection (1-", "(1-9)?", "(1-11)?", "(1-12)?",
            ]
            # Inline timed wait replacing _drain so we can print a progress line
            # every 5 minutes without blocking the operator's view.
            _m3 = None
            _boot_wait_start = time.monotonic()
            _next_progress = _boot_wait_start + 300  # first message after 5 min
            _all_boot_sigs = ["login:", "type yes to confirm and continue"] + _boot_menu_sigs_6
            _all_boot_sigs_lower = [s.lower() for s in _all_boot_sigs]
            _boot_buf = ""
            while time.monotonic() - _boot_wait_start < _boot_timeout:
                if _shutdown_event.is_set():
                    break
                _now = time.monotonic()
                if _now >= _next_progress:
                    _elapsed_min = int((_now - _boot_wait_start) / 60)
                    _remaining_min = int((_boot_timeout - (_now - _boot_wait_start)) / 60)
                    _status(f"  ⏳ [{ip}] Option 6 boot: waiting for node to boot... "
                            f"({_elapsed_min} min elapsed, {_remaining_min} minutes before timeout)")
                    if log:
                        log.log(f"[{ip}] boot wait progress: {_elapsed_min} min elapsed")
                    _next_progress = _now + 300
                    # Nudge the console so the node re-displays login: if it has
                    # already booted but recv_ready() has gone silent.
                    try:
                        ch.send("\r")
                    except OSError:
                        pass
                if ch.recv_ready():
                    _chunk = ch.recv(4096).decode("utf-8", errors="replace")
                    _boot_buf += _chunk
                    if _nf6:
                        _par_write(_nf6, _chunk)
                    _boot_buf_lower = _boot_buf.lower()
                    # Detect VLDB / cluster-node errors that indicate the node
                    # won't come online normally.
                    if ("timed out waiting for vldb online" in _boot_buf_lower
                            or "failed to get number of nodes in cluster" in _boot_buf_lower):
                        _status(f"  ⚠️  [{ip}] VLDB online timeout detected during boot wait.")
                        if log:
                            log.log(f"[{ip}] VLDB online timeout seen during option 6 boot wait", prefix="WARN")
                        if _vldb_prompt():
                            _status(f"  ✅ [{ip}] Proceeding to reinitialization.")
                            # Image is installed at this point — record
                            # install_done so --resume can skip Steps 2–6a
                            # and jump straight to the reinit reconnect.
                            with connect_lock:
                                _opt6_login_nodes.add(ip)
                            _mark_install_done(ip)
                            if _nf6:
                                with suppress(Exception):
                                    _nf6.close()
                            return
                        else:
                            _status(f"  ❌ [{ip}] Operator chose to exit after VLDB timeout.")
                            _shutdown_event.set()
                            if _nf6:
                                with suppress(Exception):
                                    _nf6.close()
                            return
                    # NVRAM sysid mismatch – node cannot complete boot; proceed to reinit.
                    if "nvram changed on this node" in _boot_buf_lower:
                        _status(f"  ⚠️  [{ip}] NVRAM sysid mismatch detected – proceeding directly to reinitialization.")
                        if log:
                            log.log(f"[{ip}] NVRAM changed / sysid mismatch seen; skipping boot wait", prefix="WARN")
                        # Image is installed at this point — record install_done
                        # so a failed reinit reconnect does not force a re-install.
                        with connect_lock:
                            _opt6_login_nodes.add(ip)
                        _mark_install_done(ip)
                        if _nf6:
                            with suppress(Exception):
                                _nf6.close()
                        return
                    for _sig in _all_boot_sigs_lower:
                        if _sig in _boot_buf_lower:
                            _m3 = _sig
                            break
                    if _m3:
                        # Auto-answer "Type yes to confirm and continue" and keep waiting.
                        if "type yes to confirm and continue" in _m3:
                            _status(f"  [{ip}] Option 6 boot confirmation prompt → yes")
                            if log:
                                log.log(f"[{ip}] option 6 boot confirmation 'type yes to confirm' → yes")
                            if _nf6:
                                _par_write(_nf6, "\n>>> yes (option 6 boot confirmation)\n")
                            try:
                                ch.send("yes\r")
                            except OSError:
                                pass
                            _m3 = None  # clear so we keep waiting for login:/boot-menu
                            _boot_buf = ""
                        else:
                            break
                    if len(_boot_buf) > 16384:
                        _boot_buf = _boot_buf[-8192:]
                time.sleep(0.1)

            # Timeout with no match – ask the operator whether to proceed.
            if not _m3:
                _status(f"  ⚠️  [{ip}] Node failed to boot within {_boot_timeout_min}-minute timeout.")
                with _stdout_lock:
                    _real_stdout.write(
                        f"\n  ⚠️  [{ip}] Node failed to boot within timeout period; "
                        f"continue with reinit? [y/n] (auto-yes in 5 min): "
                    )
                    _real_stdout.flush()
                # Read with a 5-minute auto-yes timeout so an unattended run
                # is not blocked indefinitely.
                _boot_ans_holder = [None]
                def _read_boot_ans():
                    try:
                        _boot_ans_holder[0] = sys.stdin.readline().strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        _boot_ans_holder[0] = "n"
                _ans_thread = threading.Thread(target=_read_boot_ans, daemon=True)
                _ans_thread.start()
                _ans_thread.join(timeout=300)
                if _boot_ans_holder[0] is None:
                    # Timed out – default to yes.
                    _boot_ans = "y"
                    with _stdout_lock:
                        _real_stdout.write("\n  ⏱  No response in 5 min – assuming yes.\n")
                        _real_stdout.flush()
                else:
                    _boot_ans = _boot_ans_holder[0]
                if _session_log:
                    log.log(f"[{ip}] boot timeout prompt answered: {_boot_ans}")
                if _boot_ans == "y":
                    _status(f"  ✅ [{ip}] Proceeding to reinitialization after boot timeout.")
                    if _nf6:
                        with suppress(Exception):
                            _nf6.close()
                    return
                else:
                    _status(f"  ❌ [{ip}] Operator chose to exit after boot timeout.")
                    _shutdown_event.set()
                    if _nf6:
                        with suppress(Exception):
                            _nf6.close()
                    return

            # If a boot menu appeared (node can't boot normally), send option 4.
            if _m3 and any(s in _m3.lower() for s in _boot_menu_sigs_6):
                _status(f"  ⚠️  [{ip}] Boot menu appeared after option 6 "
                        f"('Normal Boot is prohibited') – sending option 4...")
                if log:
                    log.log(f"[{ip}] boot menu seen after option 6; sending option 4", prefix="WARN")
                if _nf6:
                    _par_write(_nf6, "\n>>> boot menu appeared – draining to selection prompt\n")
                # Drain until the numbered selection prompt is fully rendered.
                if not any(s in (_m3 or "").lower() for s in ["selection (1-", "(1-9)?", "(1-11)?"]):
                    _drain(60, ["selection (1-", "(1-9)?", "(1-11)?", "(1-12)?"])
                time.sleep(1)
                if _nf6:
                    _par_write(_nf6, "\n>>> sending option 4\n")
                try:
                    ch.send("4\r")
                except OSError:
                    pass
                _status(f"  ⏳ [{ip}] Option 4 sent – auto-answering disk erase prompts...")
                if log:
                    log.log(f"[{ip}] option 4 sent after unexpected boot menu; answering disk erase prompts")

                # Option 4 shows several confirmation prompts before booting.
                # Prompts may appear in different orders or be skipped entirely,
                # so we watch for ALL remaining triggers simultaneously and
                # dispatch whichever arrives first.
                _opt4_prompts = [
                    ("this node may not have been properly unjoined",        "yes", "unjoin warning"),
                    ("encrypting drives",                                    "y",   "encrypting drives re-key warning"),
                    ("zero disks, reset config and install a new file system", "yes", "zero disks"),
                    ("this will erase all the data on the disks",            "yes", "erase data"),
                    ("type yes to confirm and continue",                     "yes", "type-yes confirm"),
                ]
                _answered = set()
                _opt4_deadline = time.monotonic() + 300  # 5 min total for all prompts
                while time.monotonic() < _opt4_deadline:
                    _remaining = [p for p in _opt4_prompts if p[2] not in _answered]
                    if not _remaining:
                        break
                    _triggers_left = [p[0] for p in _remaining] + ["login:"]
                    _ma = _drain(30, _triggers_left)
                    if not _ma:
                        continue
                    if "login:" in _ma.lower():
                        break
                    for _trigger, _answer, _lbl in _remaining:
                        if _trigger in _ma.lower():
                            _status(f"  [{ip}] {_lbl} prompt → {_answer}")
                            if log:
                                log.log(f"[{ip}] {_lbl} → {_answer}")
                            if _nf6:
                                _par_write(_nf6, f"\n>>> {_lbl} → {_answer}\n")
                            try:
                                ch.send(_answer + "\r")
                            except OSError:
                                pass
                            _answered.add(_lbl)
                            break

                # Option 4 boot: separate inline timed loop with its own timer.
                _m3 = None
                _opt4_boot_start = time.monotonic()
                _opt4_boot_timeout = 1200  # 20 minutes
                _opt4_next_progress = _opt4_boot_start + 300
                _opt4_buf = ""
                _opt4_sigs_lower = ["login:", "timed out waiting for vldb online",
                                    "failed to get number of nodes in cluster",
                                    "nvram changed on this node",
                                    "type yes to confirm and continue"]
                _status(f"  ⏳ [{ip}] Option 4 boot: waiting for node to boot (up to 20 min)...")
                if log:
                    log.log(f"[{ip}] option 4 sent; waiting for login prompt (up to 20 min)")
                while time.monotonic() - _opt4_boot_start < _opt4_boot_timeout:
                    if _shutdown_event.is_set():
                        break
                    _now4 = time.monotonic()
                    if _now4 >= _opt4_next_progress:
                        _el4 = int((_now4 - _opt4_boot_start) / 60)
                        _rm4 = int((_opt4_boot_timeout - (_now4 - _opt4_boot_start)) / 60)
                        _status(f"  ⏳ [{ip}] Option 4 boot: waiting for node to boot... "
                                f"({_el4} min elapsed, {_rm4} minutes before timeout)")
                        if log:
                            log.log(f"[{ip}] option 4 boot wait: {_el4} min elapsed")
                        _opt4_next_progress = _now4 + 300
                    if ch.recv_ready():
                        _chunk4 = ch.recv(4096).decode("utf-8", errors="replace")
                        _opt4_buf += _chunk4
                        if _nf6:
                            _par_write(_nf6, _chunk4)
                        _opt4_buf_lower = _opt4_buf.lower()
                        for _s4 in _opt4_sigs_lower:
                            if _s4 in _opt4_buf_lower:
                                _m3 = _s4
                                break
                        if _m3:
                            if "type yes to confirm and continue" in _m3:
                                _status(f"  [{ip}] Option 4 boot confirmation prompt → yes")
                                if log:
                                    log.log(f"[{ip}] option 4 boot confirmation 'type yes to confirm' → yes")
                                if _nf6:
                                    _par_write(_nf6, "\n>>> yes (option 4 boot confirmation)\n")
                                try:
                                    ch.send("yes\r")
                                except OSError:
                                    pass
                                _m3 = None
                                _opt4_buf = ""
                            elif "nvram changed on this node" in _m3:
                                _status(f"  ⚠️  [{ip}] NVRAM sysid mismatch detected during option 4 boot – proceeding to reinitialization.")
                                if log:
                                    log.log(f"[{ip}] NVRAM changed / sysid mismatch seen during option 4 boot; proceeding to reinit", prefix="WARN")
                                if _nf6:
                                    with suppress(Exception):
                                        _nf6.close()
                                return
                            else:
                                break
                        if len(_opt4_buf) > 16384:
                            _opt4_buf = _opt4_buf[-8192:]
                    time.sleep(0.1)
                if not _m3:
                    _status(f"  ⚠️  [{ip}] Option 4 boot timed out after 20 min.")
                    if log:
                        log.log(f"[{ip}] option 4 boot timed out", prefix="WARN")

            if _m3 and "timed out waiting for vldb online" in _m3.lower():
                _status(f"  ⚠️  [{ip}] VLDB online timeout detected.")
                if _vldb_prompt():
                    _status(f"  ✅ [{ip}] Proceeding to reinitialization.")
                    # Image is installed at this point — mark install_done
                    # so a failed reconnect does not force a re-install on
                    # resume.
                    with connect_lock:
                        _opt6_login_nodes.add(ip)
                    _mark_install_done(ip)
                    if _nf6:
                        with suppress(Exception):
                            _nf6.close()
                    return
                else:
                    _status(f"  ❌ [{ip}] Operator chose not to proceed. Exiting.")
                    _shutdown_event.set()
                    if _nf6:
                        with suppress(Exception):
                            _nf6.close()
                    return

            if not (_m3 and "login:" in _m3):
                _status(f"  ⚠️  [{ip}] Login prompt not seen within 20 min; node may still be booting.")
                if log:
                    log.log(f"[{ip}] login prompt not seen after option 6 (timeout)", prefix="WARN")
            else:
                _status(f"  [{ip}] Login prompt seen – waiting for varfs reboot trigger...")
                if log:
                    log.log(f"[{ip}] first login prompt seen; waiting for varfs_backup_restore reboot message")
                # Phase 1: wait (no timer) until the varfs reboot message
                # appears, which confirms a reboot is actually starting.
                # Accumulate output; send periodic Enters to keep the console
                # alive.  If the channel closes or the script is stopped we
                # fall through to the normal completion path.
                _varfs_trigger = "varfs_backup_restore: rebooting to load the new varfs"
                _pre_buf = ""
                _last_enter_pre = time.monotonic()
                _varfs_seen = False
                while True:
                    if _shutdown_event.is_set():
                        break
                    if time.monotonic() - _last_enter_pre >= 300:
                        try:
                            ch.send("\n")
                        except Exception:
                            pass
                        _last_enter_pre = time.monotonic()
                        _status(f"  [{ip}] Sending Enter (waiting for varfs reboot trigger)...")
                        if log:
                            log.log(f"[{ip}] periodic Enter sent while waiting for varfs trigger")
                    if ch.recv_ready():
                        _pchunk = ch.recv(4096).decode("utf-8", errors="replace")
                        _pre_buf += _pchunk
                        if _nf6:
                            _par_write(_nf6, _pchunk)
                        if "timed out waiting for vldb online" in _pre_buf.lower():
                            _status(f"  \u26a0\ufe0f  [{ip}] VLDB online timeout detected (post-login wait).")
                            if _vldb_prompt():
                                _status(f"  \u2705 [{ip}] Proceeding to reinitialization.")
                                # Image is installed at this point — mark
                                # install_done so resume skips install on
                                # restart even if the reconnect later fails.
                                with connect_lock:
                                    _opt6_login_nodes.add(ip)
                                _mark_install_done(ip)
                                _varfs_seen = False  # skip further waits
                            else:
                                _status(f"  \u274c [{ip}] Operator chose not to proceed. Exiting.")
                                _shutdown_event.set()
                            break
                        if _varfs_trigger in _pre_buf.lower():
                            _varfs_seen = True
                            _status(f"  [{ip}] varfs reboot trigger detected – starting 15 min watch timer...")
                            if log:
                                log.log(f"[{ip}] varfs_backup_restore reboot message seen; starting watch timer")
                            break
                        if len(_pre_buf) > 32768:
                            _pre_buf = _pre_buf[-16384:]
                    time.sleep(0.1)

                if not _varfs_seen:
                    # Shutdown/interrupt before varfs message – treat as done.
                    _status(f"  ✅ [{ip}] Option 6 complete – node is at login prompt.")
                    if log:
                        log.log(f"[{ip}] option 6 complete; no varfs reboot trigger (interrupted)")
                else:
                    # Phase 2: varfs reboot confirmed – watch up to 15 min for
                    # boot indicators, then wait for final login prompt.
                    _watch_buf = _pre_buf  # carry over any already-received data
                    _watch_start = time.monotonic()
                    _last_enter = _watch_start   # track last periodic Enter
                    _reboot_seen = False
                    while time.monotonic() - _watch_start < 900:
                        if _shutdown_event.is_set():
                            break
                        # Send a periodic Enter every 5 minutes.
                        if time.monotonic() - _last_enter >= 300:
                            try:
                                ch.send("\n")
                            except Exception:
                                pass
                            _last_enter = time.monotonic()
                            _status(f"  [{ip}] Sending Enter to refresh login prompt...")
                            if log:
                                log.log(f"[{ip}] periodic Enter sent to refresh login prompt")
                        if ch.recv_ready():
                            _wchunk = ch.recv(4096).decode("utf-8", errors="replace")
                            _watch_buf += _wchunk
                            if _nf6:
                                _par_write(_nf6, _wchunk)
                            if "timed out waiting for vldb online" in _watch_buf.lower():
                                _status(f"  \u26a0\ufe0f  [{ip}] VLDB online timeout detected (reboot-watch phase).")
                                if _vldb_prompt():
                                    _status(f"  \u2705 [{ip}] Proceeding to reinitialization.")
                                    _reboot_seen = False
                                else:
                                    _status(f"  \u274c [{ip}] Operator chose not to proceed. Exiting.")
                                    _shutdown_event.set()
                                break
                            for _ri in _reboot_indicators:
                                if _ri in _watch_buf.lower():
                                    _reboot_seen = True
                                    break
                            if _reboot_seen:
                                break
                            if len(_watch_buf) > 16384:
                                _watch_buf = _watch_buf[-8192:]
                        time.sleep(0.1)

                    if _reboot_seen:
                        _status(f"  ⏳ [{ip}] Reboot detected – waiting up to 20 min for final login prompt...")
                        if log:
                            log.log(f"[{ip}] reboot detected; waiting for second login prompt (up to 20 min)")
                        _reboot_wait_start = time.monotonic()
                        _m4 = _drain(1200, ["login:"])
                        _reboot_elapsed = time.monotonic() - _reboot_wait_start
                        _elapsed_str = f"{_reboot_elapsed / 60:.1f} min ({_reboot_elapsed:.0f}s)"
                        if _m4 and "login:" in _m4:
                            _status(f"  ✅ [{ip}] Option 6 complete – login prompt seen after {_elapsed_str}.")
                            if log:
                                log.log(f"[{ip}] option 6 complete; final login prompt seen in {_elapsed_str}")
                            with connect_lock:
                                _opt6_login_nodes.add(ip)
                            _mark_install_done(ip)
                        else:
                            _status(f"  ⚠️  [{ip}] Final login prompt not seen after {_elapsed_str}; node may still be booting.")
                            if log:
                                log.log(f"[{ip}] final login prompt not seen after reboot; waited {_elapsed_str}", prefix="WARN")
                    else:
                        _status(f"  ✅ [{ip}] Option 6 complete – node is at login prompt.")
                        if log:
                            log.log(f"[{ip}] option 6 complete; login prompt confirmed (no further reboot)")
                        with connect_lock:
                            _opt6_login_nodes.add(ip)
                        _mark_install_done(ip)

            if _nf6:
                try:
                    _nf6.close()
                    _status(f"  📝 [{ip}] Boot log saved: {_nf6.name}")
                except Exception:
                    pass
                except Exception:
                    pass

        opt6_threads = [
            threading.Thread(
                target=_select_option6,
                args=(ip, loader_channels.get(ip)),
                daemon=True,
            )
            for ip in bmc_ips
        ]
        for t in opt6_threads:
            t.start()
        for t in opt6_threads:
            t.join()

        # ── Checkpoint: mark install_done for every node that reached login ─
        # Per-node marks are already written inside _select_option6 as soon
        # as each node reaches login, so a kill mid-run preserves progress.
        # This post-join sweep is a belt-and-suspenders idempotent backstop.
        if _checkpoint:
            for _ip_ok in _opt6_login_nodes:
                if _checkpoint.is_node_done("install_done", _ip_ok):
                    continue
                try:
                    _checkpoint.mark_node_done("install_done", _ip_ok)
                    if log:
                        log.log(f"[{_ip_ok}] checkpoint: install_done saved")
                except Exception:
                    pass

        # ── Version check when all nodes are already running ONTAP ────────────
        # If every node reached login: without needing option 6, the cluster is
        # still running.  Log in via the first node's console, run 'version',
        # and let the operator confirm before wiping everything.
        if _opt6_login_nodes == set(bmc_ips):
            _ver_str = None
            _ver_ch = loader_channels.get(first_ip)
            if _ver_ch is not None:
                try:
                    import re as _re
                    _admin_pw = (_cluster_config.get("admin_password")
                                 if isinstance(_cluster_config, dict) else None)
                    # Refresh login prompt.
                    _ver_ch.send("\r")
                    time.sleep(1)
                    _vbuf = ""
                    _vt = time.monotonic()
                    while time.monotonic() - _vt < 10:
                        if _ver_ch.recv_ready():
                            _vbuf += _ver_ch.recv(4096).decode("utf-8", errors="replace")
                            if "login:" in _vbuf.lower():
                                break
                        time.sleep(0.1)
                    if "login:" in _vbuf.lower() and _admin_pw:
                        _ver_ch.send("admin\r")
                        time.sleep(0.5)
                        _vbuf2 = ""
                        _vt2 = time.monotonic()
                        while time.monotonic() - _vt2 < 10:
                            if _ver_ch.recv_ready():
                                _vbuf2 += _ver_ch.recv(4096).decode("utf-8", errors="replace")
                                if "password:" in _vbuf2.lower() or "::" in _vbuf2:
                                    break
                            time.sleep(0.1)
                        if "password:" in _vbuf2.lower():
                            _ver_ch.send(_admin_pw + "\r")
                            time.sleep(2)
                        _vbuf3 = ""
                        _vt3 = time.monotonic()
                        while time.monotonic() - _vt3 < 15:
                            if _ver_ch.recv_ready():
                                _vbuf3 += _ver_ch.recv(4096).decode("utf-8", errors="replace")
                                if "::" in _vbuf3:
                                    break
                            time.sleep(0.1)
                        if "::" in _vbuf3:
                            _ver_ch.send("version\r")
                            time.sleep(2)
                            _vbuf4 = ""
                            _vt4 = time.monotonic()
                            while time.monotonic() - _vt4 < 15:
                                if _ver_ch.recv_ready():
                                    _vbuf4 += _ver_ch.recv(4096).decode("utf-8", errors="replace")
                                    if "::" in _vbuf4:
                                        break
                                time.sleep(0.1)
                            _ver_ch.send("exit\r")
                            _vm = _re.search(r"NetApp Release\s+(\S+)", _vbuf4)
                            if _vm:
                                _ver_str = _vm.group(1)
                            else:
                                for _vl in _vbuf4.splitlines():
                                    _vl = _vl.strip()
                                    if _vl and "::" not in _vl:
                                        _ver_str = _vl
                                        break
                except Exception:
                    pass

            with _stdout_lock:
                if _ver_str:
                    _real_stdout.write(
                        f"\n  Cluster is at version \"{_ver_str}\"."
                        f" Continue with reinit? [y/n]: "
                    )
                else:
                    _real_stdout.write(
                        "\n  All nodes are at ONTAP login prompt (cluster is running)."
                        " Continue with reinit? [y/n]: "
                    )
                _real_stdout.flush()
                try:
                    _cont_ans = sys.stdin.readline().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    _cont_ans = "n"
            if log:
                log.log(f"4b: version check prompt answered '{_cont_ans}' "
                        f"(version={_ver_str!r})")
            if _cont_ans != "y":
                print("\n  Aborting reinit – install succeeded, cluster left running.")
                if log:
                    log.set_outcome("PASS", "install complete; operator chose not to reinit")
                # Clean up channels and return success (install worked fine).
                for _cip in list(loader_channels.keys()):
                    with suppress(Exception):
                        loader_channels[_cip].close()
                loader_channels.clear()
                for _cip in list(loader_clients.keys()):
                    with suppress(Exception):
                        loader_clients[_cip].close()
                loader_clients.clear()
                return True

        # Close all existing channels/clients (nodes are now at the login prompt
        # after option 6 completed).
        for ip in list(loader_channels.keys()):
            try:
                loader_channels[ip].close()
            except Exception:
                pass
        loader_channels.clear()
        for ip in list(loader_clients.keys()):
            try:
                loader_clients[ip].close()
            except Exception:
                pass
        loader_clients.clear()

        if not _do_reinit:
            return True

    # ── Skip-install equivalent of the in-block early return ──────────────
    # When the install block was skipped on resume (all nodes already at
    # ONTAP login) and the operator did not request reinit, there is nothing
    # left to do; mirror the in-block early-return so flow does not fall
    # through into Step 6b.
    if _skip_install and not _do_reinit:
        if log:
            log.log("4b resume: install already done and no reinit requested; exiting cleanly")
        return True

    # ── Step 6b: Reinit – reconnect to all BMCs and reach LOADER ──────────
    # All nodes are now at the ONTAP login prompt (4b install finished).
    # Reconnect via BMC, reset each node to LOADER, send the boot commands
    # that bring up the boot menu, and then run the selected init flow.
    print(f"\n  ✅ Netboot/install complete on all nodes. Reconnecting to "
          f"{len(bmc_ips)} BMC(s) for cluster reinit (mode {_mode_sel})...")
    if log:
        log.start_phase("4b – Reinit Reconnect to LOADER")

    _reconnect_errors = []
    _reconnect_lock = threading.Lock()

    # Pre-open one unified log file per node that spans all reinit phases
    # (reconnect-to-LOADER, boot menu, and init wizard).
    #   Primary node → 4b_node_reinit_primary_<ip>_<ts>.log
    #   Peer nodes   → 4b_node_add_<ip>_<ts>.log
    _node_reinit_logs = {}  # {ip: file_handle}
    for _ip in bmc_ips:
        _pfx = "4b_node_reinit_primary" if _ip == first_ip else "4b_node_add"
        try:
            _nf = _node_log_open(_ip, _log_dir, prefix=_pfx)
            _node_reinit_logs[_ip] = _nf
            _status(f"  📝 [{_ip}] Reinit log → {_nf.name}")
            # Populate module-level peer log paths for peer nodes only.
            if _ip != first_ip:
                _peer_log_paths[_ip] = _nf.name
        except Exception as _e:
            _status(f"  ⚠️  [{_ip}] Could not open reinit log: {_e}")
            _node_reinit_logs[_ip] = None

    def _reconnect_worker(ip, stagger_idx=0):
        # Stagger connection attempts to avoid hammering all BMCs at once,
        # which can exhaust the BMC SSH daemon capacity and cause banner
        # timeout / auth errors on simultaneous handshakes.
        if stagger_idx > 0:
            time.sleep(stagger_idx * 2)
        # Use the pre-opened unified log; do not open or close it here.
        _rl_nf = _node_reinit_logs.get(ip)
        _fb = _bmc_fallback_passwords(ip, bmc_passwords)

        # _bmc_reach_loader can fail transiently (BMC busy with a stale
        # console session, network blip, prompt detection race). Retry the
        # whole reach-LOADER sequence a few times before giving up so a
        # single transient failure does not abort the reinit.
        _reach_max = 3
        _reach_interval = 60  # seconds between attempts
        cl = ch = None
        for _attempt in range(1, _reach_max + 1):
            if _shutdown_event.is_set():
                break
            if _attempt > 1:
                _status(
                    f"  \U0001f501 [{ip}] Retrying reach-LOADER "
                    f"(attempt {_attempt}/{_reach_max}) after {_reach_interval}s..."
                )
                _waited = 0
                while _waited < _reach_interval:
                    if _shutdown_event.is_set():
                        break
                    time.sleep(1)
                    _waited += 1
                if _shutdown_event.is_set():
                    break
            cl, ch = _bmc_reach_loader(
                ip, bmc_user, bmc_passwords.get(ip, ""),
                node_log=_rl_nf, fallback_passwords=_fb,
            )
            if cl is not None and ch is not None:
                break
            if _attempt < _reach_max:
                _status(
                    f"  \u26a0\ufe0f  [{ip}] reach-LOADER attempt "
                    f"{_attempt}/{_reach_max} failed; will retry."
                )
                if log:
                    log.log(
                        f"[{ip}] reach-LOADER attempt {_attempt}/{_reach_max} "
                        "failed; retrying",
                        prefix="WARN",
                    )

        if cl is None or ch is None:
            with _reconnect_lock:
                _reconnect_errors.append(ip)
            _status(
                f"  \u274c [{ip}] Reconnect to LOADER failed after "
                f"{_reach_max} attempts."
            )
            _status(
                "     \u2139\ufe0f  Re-run this script and accept the checkpoint "
                "to continue."
            )
            return
        # From LOADER send the configured boot commands (ends with boot_ontap menu).
        for cmd in get_loader_commands():
            try:
                ch.send(cmd + "\r")
            except Exception:
                pass
            time.sleep(1)
        with _reconnect_lock:
            loader_channels[ip] = ch
            loader_clients[ip] = cl
        _status(f"  ✅ [{ip}] Reconnected – boot_ontap menu sent.")
        if _checkpoint:
            _checkpoint.mark_node_done("reinit_loader", ip)
            if log:
                log.log(f"[{ip}] checkpoint: reinit_loader saved")

    _run_parallel(bmc_ips, _reconnect_worker, with_index=True)

    if _reconnect_errors:
        print(f"\n  ⚠️  Reconnect failed for: {', '.join(_reconnect_errors)}")
        if log:
            log.log(f"4b: reinit reconnect failed: {_reconnect_errors}", prefix="WARN")
        # If the primary node failed to reconnect, there is nothing to do.
        if first_ip in _reconnect_errors:
            print(f"\n  ❌ Authentication/connection failed for primary node {first_ip}."
                  f" Aborting.")
            if log:
                log.log(f"4b: primary node {first_ip} reconnect failed; aborting",
                        prefix="ERROR")
            return False
    if log:
        log.end_phase()

    first_ch = loader_channels.get(first_ip)
    first_cl = loader_clients.get(first_ip)
    _peers_for_reinit = bmc_ips[1:] if _mode_sel == "3" else []

    if first_ch is None:
        print("\n  ❌ No channel available for first node. Cannot start reinit.")
        if log:
            log.log("4b: no channel for primary reinit after reconnect", prefix="ERROR")
        return False

    # ── Wait for post-install boot menu on primary, select 9 (modes 1/3) ──
    # Reuse the unified log already opened for the primary node.
    _pnf_primary = _node_reinit_logs.get(first_ip)

    if log:
        log.start_phase("4b – Boot Menu Selection")
    if not wait_for_boot_menu_and_select(first_ch, node_log=_pnf_primary):
        print(f"  ⚠️  [{first_ip}] Boot menu not detected; operator may need to intervene.")
        if log:
            log.log(f"[{first_ip}] boot menu not seen for primary reinit", prefix="WARN")
    if log:
        log.end_phase()

    # Checkpoint: primary cleared the ONTAP boot menu; wizard about to begin.
    if _checkpoint:
        try:
            _checkpoint.mark_done("primary_bootmenu_done")
            if log:
                log.log("4b: checkpoint: primary_bootmenu_done saved")
        except Exception:
            pass

    # ── Run primary init wizard ────────────────────────────────────────────
    # Install _NodeLogWriter on sys.stdout so all wizard/auto_complete output
    # goes to the file; only milestone lines reach the terminal.
    _primary_nlw = _NodeLogWriter(_pnf_primary, interactive=False) if _pnf_primary else None
    _prev_stdout_primary = sys.stdout
    if _primary_nlw:
        sys.stdout = _primary_nlw

    try:
        if _auto_setup:
            print(f"\n  [{first_ip}] Starting automatic cluster initialization...")
            if log:
                log.start_phase("4b – Cluster Initialization (primary)")
            auto_complete_initialization(first_ch, bmc_host=first_ip)
            if log:
                log.end_phase()
        else:
            # 1a: interactive session — pass-through mode so operator sees everything
            if _primary_nlw:
                _primary_nlw.interactive = True
            print(f"\n  [{first_ip}] Switching to interactive session...")
            if log:
                log.start_phase("4b – Interactive Session")
            session = InteractiveSession(
                first_ch, first_cl, first_ip,
                bmc_user, bmc_passwords.get(first_ip, ""),
            )
            session.run()
            if log:
                log.end_phase()
    finally:
        sys.stdout = _prev_stdout_primary

    # ── Mode 3: auto-join peer nodes ───────────────────────────────────────
    if _mode_sel == "3" and _peers_for_reinit:
        print(f"\n  Mode 3: auto-joining {len(_peers_for_reinit)} peer node(s) in parallel...")
        if log:
            log.log(f"4b: mode 3 peer auto-join for {_peers_for_reinit}")

        _peer_errors = []
        _peer_lock = threading.Lock()
        _menu_sigs_lower = [
            "selection (1-", "(1-9)?", "(1-11)?", "(1-12)?",
            # Shown after netboot install when boot device has changed:
            "use option (6) to restore the system configuration",
            "normal boot is prohibited",
        ]

        _peer_ctx = {
            "loader_channels":  loader_channels,
            "loader_clients":   loader_clients,
            "bmc_user":         bmc_user,
            "bmc_passwords":    bmc_passwords,
            "log":              log,
            "peer_errors":      _peer_errors,
            "peer_lock":        _peer_lock,
            "menu_sigs_lower":  _menu_sigs_lower,
            "status":           _status,
            "node_reinit_logs": _node_reinit_logs,
        }

        # Write node-add manifest so option 2c can resume if interrupted.
        _write_node_add_manifest(
            nodes=[
                dict(
                    bmc=_pip,
                    bmc_user=bmc_user,
                    bmc_password=bmc_passwords.get(_pip, ""),
                    **{k: v for k, v in (_node_mgmt_by_bmc.get(_pip) or {}).items()
                       if k in ("node_mgmt_ip", "node_mgmt_port",
                                "node_mgmt_netmask", "node_mgmt_gateway")},
                )
                for _pip in _peers_for_reinit
            ],
            cluster_mgmt_ip=_cluster_config.get("mgmt_ip") or "",
            cluster_admin_user=(_cluster_config.get("admin_user") or "admin"),
            cluster_admin_password=(_cluster_config.get("admin_password") or ""),
        )

        peer_threads = [
            threading.Thread(
                target=_peer_reinit_worker, args=(ip, _peer_ctx), daemon=True
            )
            for ip in _peers_for_reinit
        ]
        for t in peer_threads:
            t.start()
        for t in peer_threads:
            t.join()

        if _peer_errors:
            print(f"  ⚠️  Peer reinit issues: {_peer_errors}")

    # Close all unified per-node reinit log files now that all workers are done.
    for _ip, _nf in _node_reinit_logs.items():
        if _nf:
            try:
                _nf.close()
                _status(f"  📝 [{_ip}] Reinit log saved: {_nf.name}")
            except Exception:
                pass

    # Close first node's session.
    try:
        loader_channels[first_ip].close()
    except Exception:
        pass
    try:
        loader_clients[first_ip].close()
    except Exception:
        pass

    # Mark cluster fully formed in the checkpoint. The caller (_main) will
    # clear the checkpoint file on success.
    if _checkpoint:
        _checkpoint.mark_done("cluster_formed")
        if log:
            log.log("4b: checkpoint: cluster_formed saved")

    return True


# ---------------------------------------------------------------------------
# Mode 41 (4a): ONTAP software upgrade via rolling takeover/giveback
# ---------------------------------------------------------------------------

def _find_upgrade_package():
    """Return (source_type, value) where source_type is 'file' or 'url'.

    Search order:
      1. ONTAP/ sub-folder (relative to script dir and CWD) for *.tgz files.
      2. Interactive prompt: path to a .tgz file OR a web URL.
    Returns (None, None) if the operator enters blank (exit).
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = os.getcwd()

    tgz_files = []
    seen = set()
    for base in (script_dir, os.getcwd()):
        ontap_dir = os.path.join(base, "ONTAP")
        if os.path.isdir(ontap_dir):
            for fn in sorted(os.listdir(ontap_dir)):
                if fn.lower().endswith(".tgz"):
                    full = os.path.abspath(os.path.join(ontap_dir, fn))
                    if full not in seen:
                        tgz_files.append(full)
                        seen.add(full)

    print(
        "\n  ⚠️  WARNING: If you are downgrading FROM 9.19.1 (or later) with "
        "large-SAZ support enabled, a full cluster reinitialize will be required "
        "after reverting to an earlier ONTAP version."
    )

    if tgz_files:
        print("\n  Found upgrade package(s) in ONTAP/ folder:")
        for i, p in enumerate(tgz_files, 1):
            print(f"    {i}. {os.path.basename(p)}")
        print("    0. Enter a different path or URL")
        print("")
        while True:
            try:
                sel = input("  Select package [1] or 0 for manual entry, blank to exit: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None, None
            if sel == "":
                return None, None
            if sel == "0":
                break
            if sel.isdigit() and 1 <= int(sel) <= len(tgz_files):
                return "file", tgz_files[int(sel) - 1]
            print("  ⚠️  Out of range.")

    # Manual entry
    print("\n  Enter a path to a .tgz file or a web URL (blank to exit).")
    while True:
        try:
            ans = input("  Path or URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None, None
        if not ans:
            return None, None
        # Strip surrounding quotes
        if len(ans) >= 2 and ans[0] == ans[-1] and ans[0] in ("'", '"'):
            ans = ans[1:-1]
        if ans.lower().startswith("http://") or ans.lower().startswith("https://"):
            return "url", ans
        expanded = os.path.expanduser(os.path.expandvars(ans))
        if not os.path.isfile(expanded):
            print(f"  ⚠️  File not found: {expanded}")
            continue
        if not expanded.lower().endswith(".tgz"):
            print("  ⚠️  Only .tgz upgrade packages are supported.")
            continue
        return "file", expanded


def _start_http_server(file_path):
    """Serve a single file over HTTP/1.0 in a detached subprocess.

    The subprocess is fully independent of this process — it survives if the
    parent script exits normally, is interrupted, or crashes.  The server
    auto-exits 30 minutes after the last transfer completes (or 30 minutes
    after start if no connection ever arrives) so it does not run forever.

    Returns (None, url, server) — call server.shutdown() to stop it early.
    The first element is None (callers only store it; none join on it).

    Why a raw-socket server (same rationale as before)
    ==================================================
    SO_LINGER, TCP_NODELAY, conn.sendfile(), and SHUT_WR+drain are preserved
    in the subprocess code to guarantee graceful FIN teardown on Windows.
    """
    import sys as _sys

    file_path = os.path.abspath(file_path)
    filename  = os.path.basename(file_path)

    # ── Self-contained server code run inside the detached subprocess ──────
    # Uses only stdlib.  file_path and filename are embedded as literals via
    # the two f-string lines; all other braces belong to the subprocess code.
    _srv_code = (
        "import os,socket,struct,sys,threading,time\n"
        f"file_path={file_path!r}\n"
        f"filename={filename!r}\n"
        "IDLE_TIMEOUT=1800\n"
        "linger=(struct.pack('HH',1,60) if sys.platform=='win32'"
        " else struct.pack('ii',1,60))\n"
        "srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
        "srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "srv.bind(('',0))\n"
        "srv.listen(64)\n"
        "port=srv.getsockname()[1]\n"
        "try:\n"
        "  with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as _s:\n"
        "    _s.connect(('8.8.8.8',80));host_ip=_s.getsockname()[0]\n"
        "except Exception:host_ip='127.0.0.1'\n"
        "url=f'http://{host_ip}:{port}/{filename}'\n"
        "sys.stdout.write(url+'\\n');sys.stdout.flush()\n"
        "_active=0;_lock=threading.Lock();_last=[None]\n"
        "def handle(conn):\n"
        "  global _active\n"
        "  with _lock:_active+=1\n"
        "  try:\n"
        "    try:conn.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,linger)\n"
        "    except OSError:pass\n"
        "    try:conn.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)\n"
        "    except OSError:pass\n"
        "    conn.settimeout(15.0);req=b''\n"
        "    try:\n"
        "      while b'\\r\\n\\r\\n' not in req:\n"
        "        c=conn.recv(4096)\n"
        "        if not c:return\n"
        "        req+=c\n"
        "        if len(req)>65536:break\n"
        "    except OSError:return\n"
        "    try:fsize=os.path.getsize(file_path)\n"
        "    except OSError:\n"
        "      conn.sendall(b'HTTP/1.0 404 Not Found\\r\\nContent-Length: 0"
        "\\r\\nConnection: close\\r\\n\\r\\n');return\n"
        "    hdr=('HTTP/1.0 200 OK\\r\\nContent-Type: application/octet-stream"
        "\\r\\nContent-Length: '+str(fsize)+'\\r\\nConnection: close\\r\\n\\r\\n'"
        ").encode('ascii')\n"
        "    conn.settimeout(None);conn.sendall(hdr)\n"
        "    try:\n"
        "      with open(file_path,'rb') as fh:conn.sendfile(fh)\n"
        "    except OSError:pass\n"
        "    try:conn.shutdown(socket.SHUT_WR)\n"
        "    except OSError:pass\n"
        "    try:\n"
        "      conn.settimeout(30.0)\n"
        "      while conn.recv(8192):pass\n"
        "    except OSError:pass\n"
        "  except OSError:pass\n"
        "  finally:\n"
        "    with _lock:_active-=1;_last[0]=time.monotonic()\n"
        "    try:conn.close()\n"
        "    except OSError:pass\n"
        "srv.settimeout(1.0);_started=time.monotonic()\n"
        "while True:\n"
        "  try:\n"
        "    conn,_=srv.accept();_last[0]=time.monotonic()\n"
        "    threading.Thread(target=handle,args=(conn,)).start()\n"
        "  except socket.timeout:\n"
        "    now=time.monotonic()\n"
        "    with _lock:act=_active\n"
        "    if _last[0] is not None:\n"
        "      if act==0 and now-_last[0]>IDLE_TIMEOUT:break\n"
        "    elif now-_started>IDLE_TIMEOUT:break\n"
        "  except OSError:break\n"
        "try:srv.close()\n"
        "except OSError:pass\n"
    )

    # Launch as a detached process so it outlives the parent.
    _kw = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "stdin":  subprocess.DEVNULL,
    }
    if _sys.platform == "win32":
        _kw["creationflags"] = (subprocess.DETACHED_PROCESS
                                | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        _kw["start_new_session"] = True

    _proc = subprocess.Popen([_sys.executable, "-c", _srv_code], **_kw)
    url = _proc.stdout.readline().decode("utf-8", errors="replace").strip()
    _proc.stdout.close()

    class _Server:
        def shutdown(self):
            try:
                _proc.terminate()
                _proc.wait(timeout=5)
            except Exception:
                pass

    return None, url, _Server()


def _parse_image_show(output):
    """Parse 'system image show -iscurrent true -fields image' output.

    Returns a dict: {node_name: image_name}  e.g.
        {"node1": "image1", "node2": "image2"}
    """
    result = {}
    headers = None
    dashes_seen = False
    for line in output.splitlines():
        s = line.strip()
        if not s:
            continue
        if "::" in s or s.lower().startswith("system image"):
            continue
        if "entries were displayed" in s.lower():
            break
        if set(s) <= {"-", " "}:
            dashes_seen = True
            continue
        tokens = s.split()
        if not dashes_seen:
            lowered = [t.lower() for t in tokens]
            if "node" in lowered and "image" in lowered:
                headers = lowered
            continue
        if headers and len(tokens) >= len(headers):
            row = dict(zip(headers, tokens[:len(headers)]))
            node  = row.get("node")
            image = row.get("image")
            if node and image:
                result[node] = image
    return result


def _parse_failover_show(output):
    """Parse 'storage failover show' output, handling ONTAP's wrapped-row
    format where long node/partner names push columns onto subsequent lines.

    Returns list of dicts: [{node, partner, takeover_possible}]
    """
    rows = []
    lines = output.splitlines()

    # ── 1. Find the dashes separator and derive column boundaries ──────────
    # Each group of consecutive '-' chars maps to one column.
    col_bounds = []   # list of (start, end) tuples
    data_start = None
    for i, line in enumerate(lines):
        s = line.rstrip()
        if s and set(s) <= {"-", " "} and s.count("-") >= 4:
            j = 0
            while j < len(s):
                if s[j] == "-":
                    start = j
                    while j < len(s) and s[j] == "-":
                        j += 1
                    col_bounds.append((start, j))
                else:
                    j += 1
            data_start = i + 1
            break

    if not col_bounds or data_start is None:
        return rows

    # Column layout (by index in col_bounds):
    #   0 = Node, 1 = Partner, 2 = Takeover-Possible, 3 = State Description
    def _extract(line, col_idx):
        if col_idx >= len(col_bounds):
            return ""
        s, e = col_bounds[col_idx]
        return line[s:e].strip() if len(line) > s else ""

    # ── 2. Group data lines into logical records ────────────────────────────
    # A new record starts when a line begins with a non-space character
    # (i.e. content at the Node column). Indented lines are continuations.
    records = []
    current = []
    for line in lines[data_start:]:
        if not line.strip():
            continue
        if "entries were displayed" in line.lower():
            break
        if "::" in line:
            continue
        if line[0] != " ":
            if current:
                records.append(current)
            current = [line]
        else:
            if current:
                current.append(line)
    if current:
        records.append(current)

    # ── 3. Extract fields from each record ──────────────────────────────────
    for rec in records:
        first = rec[0]

        # Node name: always the first token of the first line.
        node = first.split()[0] if first.strip() else ""

        # Determine whether the first line contains only the node name
        # (ONTAP wraps partner/possible to the next line when the node name
        # is longer than its column width).  When a hostname has no spaces,
        # and the line has no internal space, it's node-name-only.
        first_is_node_only = " " not in first.rstrip()

        partner = ""
        possible_str = ""

        if not first_is_node_only:
            # Short node name — all columns fit on one line.
            partner     = _extract(first, 1)
            possible_str = _extract(first, 2)

        # Accumulate partner fragments and possible value from all
        # continuation lines (and first line if it had multiple columns).
        for ln in rec[1:]:
            p = _extract(ln, 1)
            if p:
                partner += p
            if not possible_str:
                v = _extract(ln, 2)
                if v:
                    possible_str = v

        tp = possible_str.lower() == "true"
        if node and partner:
            rows.append({"node": node, "partner": partner,
                         "takeover_possible": tp})

    return rows


def _wait_for_failover_state(channel, node, target_substr, total_timeout=1800,
                             poll_interval=180, log=None, phase_label=None):
    """Poll 'storage failover show -fields node,state-description' until
    the row whose first column is exactly `node` contains `target_substr`
    (case-insensitive) in the state-description column.
    Returns True on success, False on timeout.

    Console output is suppressed during the poll — the raw cluster command
    echo and full state row land in the session log only. The operator
    sees a single recurring "Waiting for ..." line with elapsed/remaining
    seconds.
    """
    import time as _time
    label = phase_label or f"failover state '{target_substr}'"
    start = _time.monotonic()
    while True:
        elapsed = _time.monotonic() - start
        if elapsed >= total_timeout:
            break
        remaining = total_timeout - elapsed
        with _suppress_console():
            out = _run_cluster_command(
                channel,
                "set advanced -c off; storage failover show -fields node,state-description",
                timeout=30,
            )
        matched_row = None
        for line in out.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            first_token = stripped.split()[0].lower()
            if first_token != node.lower():
                continue
            matched_row = stripped
            if target_substr.lower() in stripped.lower():
                if log:
                    log.log(f"Failover state for {node}: matched '{target_substr}'")
                return True
            break   # only one row per node; no need to keep scanning
        if log and matched_row:
            log.log(f"Failover state for {node}: {matched_row}")
        print(f"  \u23f3 Waiting for {label} on {node}  "
              f"(elapsed {int(elapsed)}s / remaining {int(remaining)}s)")
        _time.sleep(min(poll_interval, max(1, remaining)))
    if log:
        log.log(f"Timeout waiting for failover state '{target_substr}' on {node}",
                prefix="WARN")
    return False


def _pick_bmc_from_existing_config():
    """4a helper: scan for known config / BMC files and let the operator
    pick a single BMC (with its stored credentials) instead of typing the
    hostname.

    Returns (bmc_host, bmc_user, bmc_pass) when a BMC is chosen, or
    (None, None, None) when no usable config file was found or the
    operator opts for manual entry.
    """
    import json as _json

    # Build candidate list: reinit-style configs first, then any BMC_IP.json
    # / *.json containing netboot_bmcs or a nodes/primary_node section.
    _candidates = []  # list of (path, data, [(label, ip, user, pw), ...])
    _seen = set()

    def _entries_from(data):
        ents = []
        _pn = data.get("primary_node")
        if isinstance(_pn, dict) and _pn.get("bmc"):
            ents.append(("primary", str(_pn["bmc"]),
                         _pn.get("bmc_user") or "admin",
                         _pn.get("bmc_password") or ""))
        for _sn in (data.get("secondary_nodes") or []):
            if isinstance(_sn, dict) and _sn.get("bmc"):
                ents.append(("secondary", str(_sn["bmc"]),
                             _sn.get("bmc_user") or "admin",
                             _sn.get("bmc_password") or ""))
        if not ents and isinstance(data.get("nodes"), list):
            for _i, _n in enumerate(data["nodes"]):
                if isinstance(_n, dict) and _n.get("bmc"):
                    role = "primary" if _i == 0 else "secondary"
                    ents.append((role, str(_n["bmc"]),
                                 _n.get("bmc_user") or "admin",
                                 _n.get("bmc_password") or ""))
        if not ents and isinstance(data.get("netboot_bmcs"), list):
            for _ip in data["netboot_bmcs"]:
                if _ip:
                    ents.append(("bmc", str(_ip), "", ""))
        return ents

    # First pass: canonical reinit-config filenames anywhere on the path.
    for p in _find_config_files(deep_scan=True):
        ap = os.path.abspath(p)
        if ap in _seen:
            continue
        try:
            with open(p, "r", encoding="utf-8") as _f:
                _d = _json.load(_f)
        except Exception:
            continue
        if not isinstance(_d, dict):
            continue
        _ents = _entries_from(_d)
        if _ents:
            _candidates.append((p, _d, _ents))
            _seen.add(ap)

    # Second pass: BMC_IP.json (the address-only file produced by 4f /
    # _collect_netboot_bmcs). Restricted to that exact filename so we
    # don't surface arbitrary unrelated JSONs in the picker.
    for d in _default_config_search_dirs():
        p = os.path.join(d, "BMC_IP.json")
        ap = os.path.abspath(p)
        if ap in _seen or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as _f:
                _d = _json.load(_f)
        except Exception:
            continue
        if not isinstance(_d, dict):
            continue
        _ents = _entries_from(_d)
        if _ents:
            _candidates.append((p, _d, _ents))
            _seen.add(ap)

    if not _candidates:
        return None, None, None

    # Pick which file to use.
    if len(_candidates) == 1:
        _path, _data, _ents = _candidates[0]
        print(f"\n  📄 Using BMC list from {_path}")
    else:
        print("\n  📄 Found multiple config file(s) with BMC entries:")
        for _i, (_p, _, _e) in enumerate(_candidates, 1):
            print(f"    {_i}. {_p}  ({len(_e)} BMC(s))")
        print("    0. Enter BMC address manually")
        while True:
            _sel = _prompt("  Select config file [1]: ", "1")
            if _sel == "0":
                return None, None, None
            if _sel.isdigit() and 1 <= int(_sel) <= len(_candidates):
                _path, _data, _ents = _candidates[int(_sel) - 1]
                print(f"  ✅ Loaded {_path}")
                break
            print("  ⚠️  Invalid selection.")

    # Pick which BMC from the chosen file.
    print("\n  Select the BMC to use for this upgrade:")
    for _i, (_role, _ip, _u, _pw) in enumerate(_ents, 1):
        _cred_note = f"  (user={_u or 'admin'}{', pwd from file' if _pw else ''})"
        print(f"    {_i}. {_ip:<18} [{_role}]{_cred_note}")
    print("    0. Enter BMC address manually")
    while True:
        _sel = _prompt(f"  Select BMC [1-{len(_ents)}, or 0]: ", "1")
        if _sel == "0":
            return None, None, None
        if _sel.isdigit() and 1 <= int(_sel) <= len(_ents):
            _role, _ip, _u, _pw = _ents[int(_sel) - 1]
            _u = _u or "admin"
            if not _pw:
                _pw = getpass.getpass(f"  BMC password for {_u}@{_ip}: ")
            else:
                print(f"  🔑 Using password from config for {_u}@{_ip}.")
            # Stash the picked config so downstream lookups (e.g. per-node
            # management IPs for parallel image updates) can default from
            # it without re-prompting. Promote any reinit-style file —
            # node entries are useful even without a top-level `cluster`
            # block.
            if isinstance(_data, dict) and (
                _data.get("primary_node")
                or _data.get("secondary_nodes")
                or _data.get("nodes")
                or _data.get("cluster")
            ):
                global _config_data
                _config_data = _data
                _cl_mgmt = (_data.get("cluster") or {}).get("clus_mgmt_address")
                if _cl_mgmt:
                    _cluster_config.setdefault("mgmt_ip", _cl_mgmt)
            return _ip, _u, _pw
        print("  ⚠️  Invalid selection.")


def _run_ontap_upgrade(log):
    """Full ONTAP upgrade workflow (mode 41 / option 4a).

    `log` is a SessionLogger instance (may be None in tests).
    Returns True on success, False on any fatal error.
    """
    _print_banner("\U0001f4e6 ONTAP Software Upgrade (4a)")
    print("\n  Note: only upgrades are supported (not downgrades).\n")

    # ── Step 1: locate upgrade package ─────────────────────────────────────
    src_type, src_value = _find_upgrade_package()
    if src_type is None:
        print("\n  No package selected. Exiting.")
        return False

    _prestage_ans = _prompt("\n  Do you want to pre-stage the image only? [y/N]: ", "n").lower()
    _prestage_only = (_prestage_ans == "y")
    if log:
        log.log(f"Pre-stage only: {_prestage_only}")

    httpd = None
    pkg_url = None
    try:
        if src_type == "file":
            print(f"\n  \U0001f4e6 Package: {src_value}")
            print("  \U0001f310 Starting temporary HTTP server to host the file...")
            if log:
                log.log(f"Upgrade package: {src_value}")
            _srv_thread, pkg_url, httpd = _start_http_server(src_value)
            print(f"  \U0001f517 Package URL: {pkg_url}")
            if log:
                log.log(f"HTTP server started; package URL: {pkg_url}")
        else:
            pkg_url = src_value
            print(f"  \U0001f517 Package URL: {pkg_url}")
            if log:
                log.log(f"Upgrade package URL (user-supplied): {pkg_url}")

        # ── Step 2: BMC credentials ─────────────────────────────────────────
        print("")
        print("  " + "\u2500" * 58)
        # Prefer an existing BMC/reinit config file (numbered list) over
        # asking the operator to type the address. Falls back to the manual
        # prompts when nothing usable is on disk or the operator declines.
        bmc_host, bmc_user, bmc_pass = _pick_bmc_from_existing_config()
        if not bmc_host:
            bmc_host = input("  BMC hostname / IP: ").strip()
            if not bmc_host:
                print("  No BMC specified. Exiting.")
                return False
            bmc_user = input("  BMC username [admin]: ").strip() or "admin"
            bmc_pass = getpass.getpass(f"  BMC password for {bmc_user}@{bmc_host}: ")
        if log:
            log.log(f"4a: BMC selected: {bmc_user}@{bmc_host}")

        # ── Step 3: SSH to BMC ──────────────────────────────────────────────
        if log:
            log.start_phase("BMC Connection")
        print(f"\n  \U0001f50c Connecting to BMC {bmc_host}...")
        try:
            client_41, bmc_user, bmc_pass = _ssh_connect_with_retry(
                bmc_host, bmc_user, bmc_pass,
                label="upgrade/BMC", max_attempts=5, interactive=True,
            )
        except Exception as e:
            print(f"\n  \u274c BMC connection failed: {e}")
            if log:
                log.log(f"BMC connection failed: {e}", prefix="ERROR")
            return False
        channel_41 = _open_shell(client_41)
        # Publish the BMC credentials as the "primary" creds so the silent
        # cluster-login candidates (_candidate_cluster_logins) can reuse them
        # when the console hits a `login:` prompt; otherwise the operator
        # would be re-prompted for username/password they already supplied.
        global _primary_bmc_user, _primary_bmc_password
        _primary_bmc_user = bmc_user
        _primary_bmc_password = bmc_pass
        if log:
            log.end_phase()

        # ── Step 4: BMC prompt + system console ─────────────────────────────
        if not wait_for_bmc_prompt(channel_41, auto_takeover=True):
            print("  \u274c BMC prompt not received. Exiting.")
            if log:
                log.log("BMC prompt not received", prefix="ERROR")
            return False

        if log:
            log.start_phase("Cluster Shell Login")
        # Suppress raw BMC/console echo while we walk through `system console`
        # and the cluster login prompt — the chunks ("system console", "Type
        # Ctrl-D to exit.", "login:", etc.) are still captured in the session
        # log, but the operator's terminal stays readable.
        print("  \U0001f4fa Entering system console (output suppressed; "
              "see log file)...")
        with _suppress_console():
            enter_system_console(channel_41)

        # ── Step 5: cluster shell login ─────────────────────────────────────
        # _attempt_console_cluster_login (invoked from inside
        # _wait_for_cluster_prompt when a `login:` prompt appears) will try
        # _primary_bmc_user/_primary_bmc_password automatically, so the
        # operator should not see a prompt at all in the common case.
        with _suppress_console():
            _cluster_up = _wait_for_cluster_prompt(channel_41, timeout=60)
        if not _cluster_up:
            print(f"  ℹ️  Cluster prompt not detected; trying BMC credentials "
                  f"({bmc_user}) for cluster login...")
            if log:
                log.log(f"Cluster login: reusing BMC credentials ({bmc_user})")
            if not _login_primary_cluster_shell(channel_41, bmc_pass):
                print("  ⚠️  Cluster login with BMC credentials failed; "
                      "prompting for cluster admin password...")
                try:
                    cl_pass = getpass.getpass(
                        f"  Cluster admin password for {bmc_user}: "
                    )
                except (EOFError, KeyboardInterrupt):
                    return False
                if not _login_primary_cluster_shell(channel_41, cl_pass):
                    print("  \u274c Cluster shell login failed. Exiting.")
                    if log:
                        log.log("Cluster shell login failed", prefix="ERROR")
                    return False
        if log:
            log.end_phase()

        print("  \u2705 Logged in to cluster shell.")

        # ── Step 6: determine current image per node ────────────────────────
        if log:
            log.start_phase("Upgrade Workflow")
        print("\n  \U0001f50d Querying current images per node...")
        with _suppress_console():
            out_img = _run_cluster_command(
                channel_41,
                "set advanced -c off; system image show -iscurrent true -fields image",
                timeout=60,
            )
        node_image = _parse_image_show(out_img)
        if not node_image:
            print("  \u274c Could not parse 'system image show' output. Exiting.")
            if log:
                log.log("Failed to parse system image show output", prefix="ERROR")
            return False

        print("\n  Current images:")
        for n, img in sorted(node_image.items()):
            print(f"    {n:30s}  {img}")
        if log:
            log.log(f"Current images: {node_image}")

        # Group nodes by which image they are currently running.
        # The replacement image for each node is the OTHER image slot.
        image_to_nodes = {}
        for node, img in node_image.items():
            image_to_nodes.setdefault(img, []).append(node)

        # ── Step 7: promoted-dev-update per node (must run before image update) ──
        print("\n  \U0001f504 Running promoted-dev-update on all nodes...")
        for nodename in node_image:
            print(f"  \u27a1\ufe0f  promoted-dev-update: {nodename}...")
            if log:
                log.log(f"Running promoted-dev-update on {nodename}")
            with _suppress_console():
                _run_cluster_command(
                    channel_41,
                    f"set diag -c off; system node image promoted-dev-update -node {nodename}",
                    timeout=120,
                )
            print(f"  \u2705 promoted-dev-update complete for {nodename}.")
            if log:
                log.log(f"promoted-dev-update complete for {nodename}")

        # ── Step 8: validate then run image update per group ────────────────
        # Ask once whether to run updates in parallel via separate SSH sessions.
        print("")
        print("  " + "\u2500" * 58)
        _par_ans = _prompt("  Update nodes in parallel? [y/N]: ", "n").lower()
        _parallel_update = (_par_ans == "y")

        _cl_mgmt_ip = None
        _cl_admin_user = "admin"
        _cl_admin_pass = ""
        # Map of ontap-nodename -> mgmt IP used for the parallel SSH session.
        # Empty when sequential mode is selected.
        _node_ssh_targets = {}
        if _parallel_update:
            # Prefer per-node management IPs from the picked reinit config so
            # each parallel worker hits its own node instead of funneling
            # every session through the cluster-mgmt LIF. Falls back to
            # scanning every reinit config on disk, then finally to a
            # manual prompt.
            import json as _json_pool
            _cfg_cluster = (_config_data.get("cluster") or {}) if isinstance(_config_data, dict) else {}
            _node_mgmt_pool = []  # ordered list of mgmt IPs from config
            _seen_ips = set()

            def _harvest_pool(_d):
                if not isinstance(_d, dict):
                    return
                _pn = _d.get("primary_node") or {}
                if isinstance(_pn, dict) and _pn.get("node_mgmt_ip"):
                    _ip = str(_pn["node_mgmt_ip"]).strip()
                    if _ip and _ip not in _seen_ips:
                        _seen_ips.add(_ip)
                        _node_mgmt_pool.append(_ip)
                for _sn in (_d.get("secondary_nodes") or []):
                    if isinstance(_sn, dict) and _sn.get("node_mgmt_ip"):
                        _ip = str(_sn["node_mgmt_ip"]).strip()
                        if _ip and _ip not in _seen_ips:
                            _seen_ips.add(_ip)
                            _node_mgmt_pool.append(_ip)
                for _n in (_d.get("nodes") or []):
                    if isinstance(_n, dict) and _n.get("node_mgmt_ip"):
                        _ip = str(_n["node_mgmt_ip"]).strip()
                        if _ip and _ip not in _seen_ips:
                            _seen_ips.add(_ip)
                            _node_mgmt_pool.append(_ip)

            _harvest_pool(_config_data if isinstance(_config_data, dict) else None)
            if not _node_mgmt_pool:
                # The picked file may have been a BMC_IP.json with no node
                # mgmt info; widen the search to every reinit config on
                # disk so we still find node_mgmt_ip values without
                # prompting the operator.
                for _p in _find_config_files(deep_scan=True):
                    try:
                        with open(_p, "r", encoding="utf-8") as _f:
                            _harvest_pool(_json_pool.load(_f))
                    except Exception:
                        continue

            _cl_admin_user = bmc_user
            _cl_admin_pass = bmc_pass

            if _node_mgmt_pool:
                print(f"\n  \U0001f4cd Using {len(_node_mgmt_pool)} node management "
                      f"IP(s) from reinit config for parallel SSH:")
                for _ip in _node_mgmt_pool:
                    print(f"     • {_ip}")
                if log:
                    log.log(f"4a: node mgmt IPs from config: {_node_mgmt_pool}")
                _validate_targets = list(_node_mgmt_pool)
            else:
                # No node mgmt IPs anywhere on disk — last-resort manual
                # entry. Operator can still type a single LIF (cluster or
                # node mgmt) to drive all parallel sessions.
                print("\n  \u26a0\ufe0f  No node management IPs found in any reinit "
                      "config on disk.")
                _manual_ip = _prompt(
                    "  Enter a node (or cluster) management IP to use "
                    "for all parallel sessions: "
                )
                if not _manual_ip:
                    print("  No address entered; falling back to sequential.")
                    _parallel_update = False
                    _validate_targets = []
                else:
                    _cl_mgmt_ip = _manual_ip
                    print(f"  \U0001f4cd Using management IP: {_cl_mgmt_ip}")
                    if log:
                        log.log(f"4a: parallel SSH fallback IP: {_cl_mgmt_ip}")
                    _validate_targets = [_cl_mgmt_ip]

            # Ping + auth validation: confirm every target is reachable and
            # accepts the BMC credentials BEFORE starting the long-running
            # parallel update. A single unreachable LIF here is much easier
            # to fix now than mid-install.
            if _parallel_update and _validate_targets:
                print(f"\n  \U0001f50d Validating reachability and authentication "
                      f"({_cl_admin_user}) on {len(_validate_targets)} target(s)...")
                _bad = []
                for _t in _validate_targets:
                    # Quick TCP/22 probe first (3s) so unreachable LIFs fail
                    # fast without the multi-attempt SSH retry storm.
                    _reach_ok = False
                    try:
                        _s = socket.create_connection((_t, 22), timeout=3)
                        _s.close()
                        _reach_ok = True
                    except Exception as _re:
                        _bad.append((_t, f"TCP/22 unreachable: {_re}"))
                        print(f"     \u274c {_t:<18} not reachable on TCP/22 ({_re})")
                        continue
                    # Auth check via a one-shot SSH connect.
                    try:
                        _vcl, _, _ = _ssh_connect_with_retry(
                            _t, _cl_admin_user, _cl_admin_pass,
                            label=f"validate/{_t}", max_attempts=1,
                            interactive=False,
                        )
                        try:
                            _vcl.close()
                        except Exception:
                            pass
                        print(f"     \u2705 {_t:<18} reachable; auth OK")
                    except Exception as _ae:
                        _bad.append((_t, f"SSH auth failed: {_ae}"))
                        print(f"     \u274c {_t:<18} SSH auth failed ({_ae})")

                if _bad:
                    print(f"\n  \u26a0\ufe0f  {len(_bad)} target(s) failed validation:")
                    for _ip, _why in _bad:
                        print(f"     • {_ip}: {_why}")
                    if log:
                        log.log(f"4a: parallel target validation failures: {_bad}",
                                prefix="ERROR")
                    _ans = _prompt(
                        "  Proceed anyway with only the healthy target(s)? "
                        "[y/N]: ", "n"
                    ).lower()
                    if _ans != "y":
                        print("  Falling back to sequential mode via system console.")
                        _parallel_update = False
                    else:
                        _validate_targets = [_t for _t in _validate_targets
                                             if _t not in [b[0] for b in _bad]]
                        if not _validate_targets:
                            print("  No healthy targets; falling back to sequential.")
                            _parallel_update = False

            # Build the per-node SSH target mapping by round-robin assigning
            # the surviving targets to the ONTAP update tasks. Any single
            # node-mgmt IP can issue `system image update -node <nn>`, so the
            # mapping just spreads load across reachable LIFs. Built once
            # _update_tasks is known (below); stash the survivor list here.
            _ssh_target_pool = list(_validate_targets) if _parallel_update else []
        if log:
            log.log(f"Image update mode: {'parallel via {_cl_mgmt_ip}' if _parallel_update else 'sequential via console'}")

        print("\n  \U0001f4e4 Installing upgrade package to all nodes...")

        # Build flat list of (nodename, replace_img) tasks in group order.
        _update_tasks = []
        for current_img, group_nodes in sorted(image_to_nodes.items()):
            replace_img = "image2" if current_img == "image1" else "image1"
            for nodename in group_nodes:
                _update_tasks.append((nodename, replace_img, current_img))

        # Round-robin assign the validated SSH targets to the update tasks
        # so each parallel worker hits a different node-mgmt LIF when
        # multiple are available. Deferred until _update_tasks is built.
        if _parallel_update and _ssh_target_pool:
            for _i, (nodename, _, _) in enumerate(_update_tasks):
                _node_ssh_targets[nodename] = _ssh_target_pool[_i % len(_ssh_target_pool)]
            if log:
                log.log(f"4a: per-node SSH target map: {_node_ssh_targets}")

        if _parallel_update:
            # ── Parallel helper ────────────────────────────────────────────
            # Each worker opens its own SSH connection → invoke_shell so
            # ONTAP sees a proper interactive CLI session.  Output is
            # collected into a per-thread local buffer; nothing is written
            # to shared sys.stdout during the send/recv loop, eliminating
            # the interleaving that plagued earlier exec_command attempts.
            # Prompt regex is now module-level (_SHELL_PROMPT_RE) so it is
            # compiled once at import time, not on every parallel update.

            def _shell_run_cmd(cl, cmd, timeout=960):
                """Open an invoke_shell on `cl`, wait for ::>, send `cmd`,
                collect output until the next ::> prompt, return the output.
                Raises RuntimeError on failure.

                Polling uses adaptive sleep: 10 ms while data is actively
                flowing, 100 ms while idle.  This trims ~90 ms of latency
                from each prompt match without spinning the CPU when the
                cluster is quiet.
                """
                ch = _open_shell(cl, width=220, height=50)
                buf = ""
                deadline = time.monotonic() + 30
                got_prompt = False
                while time.monotonic() < deadline:
                    if ch.recv_ready():
                        buf += ch.recv(4096).decode("utf-8", errors="replace")
                        if _SHELL_PROMPT_RE.search(buf[-200:]):
                            got_prompt = True
                            break
                        time.sleep(0.01)
                    else:
                        time.sleep(0.1)
                if not got_prompt:
                    ch.close()
                    raise RuntimeError("Cluster prompt not seen after login")
                buf = ""
                ch.send(cmd + "\r")
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if ch.recv_ready():
                        buf += ch.recv(4096).decode("utf-8", errors="replace")
                        if _SHELL_PROMPT_RE.search(buf[-200:]):
                            time.sleep(0.3)
                            while ch.recv_ready():
                                buf += ch.recv(4096).decode("utf-8", errors="replace")
                            break
                        time.sleep(0.01)
                    else:
                        time.sleep(0.1)
                ch.close()
                return buf

            # ── Parallel validation ────────────────────────────────────────
            print("\n  \U0001f504 Running validation on all nodes in parallel...")
            if log:
                log.log(f"Starting parallel validation on: {[t[0] for t in _update_tasks]}")
            _val_results = {}
            _val_threads = []
            for nodename, replace_img, _ci in _update_tasks:
                def _val_worker(nn=nodename, ri=replace_img, rd=_val_results):
                    label_v = f"validate/{nn}"
                    _target = _node_ssh_targets.get(nn, _cl_mgmt_ip)
                    try:
                        clv, _, _ = _ssh_connect_with_retry(
                            _target, _cl_admin_user, _cl_admin_pass,
                            label=label_v, max_attempts=3, interactive=False,
                        )
                    except Exception as ev:
                        rd[nn] = (False, f"SSH connect failed: {ev}")
                        return
                    try:
                        vcmd2 = (
                            f"set advanced -c off; system image update -node {nn} "
                            f"-package {pkg_url} "
                            f"-replace {ri} "
                            f"-replace-package true "
                            f"-setdefault true "
                            f"-validate-only true"
                        )
                        print(f"  [{nn}] \U0001f50e Validating...")
                        out_v = _shell_run_cmd(clv, vcmd2, timeout=360)
                        failed_v = "error" in out_v.lower() or "failed" in out_v.lower()
                        rd[nn] = (not failed_v, out_v)
                        status = "\u274c Failed" if failed_v else "\u2705 Passed"
                        print(f"  [{nn}] Validation {status}")
                    except Exception as ev:
                        rd[nn] = (False, f"Exception: {ev}")
                        print(f"  [{nn}] \u274c Validation exception: {ev}")
                    finally:
                        try:
                            clv.close()
                        except Exception:
                            pass

                tv = threading.Thread(target=_val_worker, daemon=True)
                _val_threads.append(tv)
                tv.start()

            for tv in _val_threads:
                tv.join()

            # Show summary and prompt once for all nodes.
            val_failures = [(nn, msg) for nn, (ok, msg) in _val_results.items() if not ok]
            val_passes   = [(nn, msg) for nn, (ok, msg) in _val_results.items() if ok]
            print(f"\n  Validation summary: {len(val_passes)} passed, {len(val_failures)} failed.")
            if val_failures:
                print("  \u274c Failed nodes:")
                for nn, msg in val_failures:
                    print(f"    {nn}:\n{msg[-400:]}")
                if log:
                    log.log(f"Validation failures: {[nn for nn, _ in val_failures]}", prefix="ERROR")
                ans = _prompt("\n  Validation failed on one or more nodes; continue with upgrade? [y/N]: ", "n").lower()
                if log:
                    log.log(f"User chose to {'continue' if ans == 'y' else 'stop'} after validation failures")
                if ans != "y":
                    print("  Exiting.")
                    return False
            else:
                if log:
                    log.log("All nodes passed validation")
                ans = _prompt("\n  Validation succeeded on all nodes; continue with upgrade? [Y/n]: ", "n").lower()
                if log:
                    log.log(f"User chose to {'continue' if ans != 'n' else 'stop'} after validation success")
                if ans == "n":
                    print("  Exiting.")
                    return False

            # ── Parallel install ───────────────────────────────────────────
            print(f"\n  \U0001f680 Starting parallel image install on {len(_update_tasks)} node(s)...")
            if log:
                log.log(f"Starting parallel image install on: {[t[0] for t in _update_tasks]}")
            _par_results = {}
            _par_threads = []
            for nodename, replace_img, _ci in _update_tasks:

                def _par_worker(nn=nodename, ri=replace_img, rd=_par_results):
                    label2 = f"upgrade/{nn}"
                    _target = _node_ssh_targets.get(nn, _cl_mgmt_ip)
                    try:
                        cl2, _, _ = _ssh_connect_with_retry(
                            _target, _cl_admin_user, _cl_admin_pass,
                            label=label2, max_attempts=3, interactive=False,
                        )
                    except Exception as e2:
                        rd[nn] = (False, f"SSH connect failed: {e2}")
                        return
                    try:
                        ucmd2 = (
                            f"set advanced -c off; system image update -node {nn} "
                            f"-package {pkg_url} "
                            f"-replace {ri} "
                            f"-replace-package true "
                            f"-setdefault true"
                        )
                        print(f"  [{nn}] \U0001f4e5 Installing image...")
                        out_u2 = _shell_run_cmd(cl2, ucmd2, timeout=960)
                        # Full installer output (tar listings, MD5 / firmware
                        # progress, prompts) is verbose; log it but keep the
                        # console to a one-line status per node.
                        if log:
                            log.log(f"[{nn}] image install output (tail):\n{out_u2[-2000:]}")
                        upd_failed2 = "error" in out_u2.lower() or "failed" in out_u2.lower()
                        if upd_failed2:
                            rd[nn] = (False, out_u2[-500:])
                            print(f"  [{nn}] \u274c Install failed (see log for details).")
                        else:
                            rd[nn] = (True, "")
                            print(f"  [{nn}] \u2705 Image installed.")
                    except Exception as e2:
                        rd[nn] = (False, f"Exception: {e2}")
                        print(f"  [{nn}] \u274c Exception: {e2}")
                    finally:
                        try:
                            cl2.close()
                        except Exception:
                            pass

                t2 = threading.Thread(target=_par_worker, daemon=True)
                _par_threads.append(t2)
                t2.start()

            for t2 in _par_threads:
                t2.join()

            # Check results.
            install_errors = []
            for nodename, _, _ in _update_tasks:
                res = _par_results.get(nodename, (False, "No result recorded"))
                ok, msg = res[0], res[1]
                if not ok:
                    install_errors.append(f"  {nodename}: {msg}")
                    if log:
                        log.log(f"Parallel install failed for {nodename}: {msg}", prefix="ERROR")
            if install_errors:
                print("\n  \u274c One or more parallel installs failed:")
                for e in install_errors:
                    print(e)
                return False
            print("\n  \u2705 All parallel installs complete.")
            if log:
                log.log("All parallel image installs complete")

        else:
            # ── Sequential path (original behaviour) ──────────────────────
            for nodename, replace_img, current_img in _update_tasks:
                print(f"\n  \u27a1\ufe0f  Node: {nodename}  (current={current_img}, replace={replace_img})")
                if log:
                    log.log(f"Updating node {nodename}: replace={replace_img} pkg={pkg_url}")

                vcmd = (
                    f"set advanced -c off; system image update -node {nodename} "
                    f"-package {pkg_url} "
                    f"-replace {replace_img} "
                    f"-replace-package true "
                    f"-setdefault true "
                    f"-validate-only true"
                )
                print(f"  \U0001f50e Validating update on {nodename}...")
                with _suppress_console():
                    out_val = _run_cluster_command(channel_41, vcmd, timeout=300)
                if "error" in out_val.lower() or "failed" in out_val.lower():
                    print(f"\n  \u274c Validation failed for {nodename}:\n{out_val}")
                    if log:
                        log.log(f"Validation failed for {nodename}: {out_val[-500:]}", prefix="ERROR")
                    ans = _prompt("\n  Validation failed; continue with upgrade? [y/N]: ", "n").lower()
                    if log:
                        log.log(f"User chose to {'continue' if ans == 'y' else 'stop'} after validation failure")
                    if ans != "y":
                        print("  Exiting.")
                        return False
                else:
                    print(f"  \u2705 Validation passed for {nodename}.")
                    if log:
                        log.log(f"Validation passed for {nodename}")
                    ans = _prompt("\n  Validation succeeded; continue with upgrade? [Y/n]: ", "n").lower()
                    if log:
                        log.log(f"User chose to {'continue' if ans != 'n' else 'stop'} after validation success")
                    if ans == "n":
                        print("  Exiting.")
                        return False

                ucmd = (
                    f"set advanced -c off; system image update -node {nodename} "
                    f"-package {pkg_url} "
                    f"-replace {replace_img} "
                    f"-replace-package true "
                    f"-setdefault true"
                )
                print(f"  \U0001f4e5 Downloading/installing image on {nodename} (may take several minutes)...")
                with _suppress_console():
                    out_upd = _run_cluster_command(channel_41, ucmd, timeout=900)
                if "error" in out_upd.lower() or "failed" in out_upd.lower():
                    print(f"\n  \u274c Image update failed for {nodename}:\n{out_upd}")
                    if log:
                        log.log(f"Image update failed for {nodename}: {out_upd[-500:]}", prefix="ERROR")
                    return False
                print(f"  \u2705 Image installed on {nodename}.")
                if log:
                    log.log(f"Image installed on {nodename}")

        # ── Pre-stage only: exit here after installs complete ────────────────
        if _prestage_only:
            print("\n  \u2705 Pre-stage complete. Image(s) installed on all nodes.")
            print("     Rolling upgrade (takeover/giveback) was skipped.")
            if log:
                log.log("Pre-stage only: exiting after image install, skipping rolling upgrade")
            return True

        # ── Step 9: verify default image ────────────────────────────────────
        print("\n  \U0001f50d Verifying default image setting...")
        with _suppress_console():
            out_def = _run_cluster_command(
                channel_41,
                "set advanced -c off; system image show -isdefault true -fields image",
                timeout=60,
            )
        default_images = _parse_image_show(out_def)
        upgrade_errors = []
        for nodename, current_img in node_image.items():
            expected_default = "image2" if current_img == "image1" else "image1"
            actual = default_images.get(nodename, "")
            if actual.lower() != expected_default.lower():
                upgrade_errors.append(
                    f"  {nodename}: expected default={expected_default}, got={actual!r}"
                )
        if upgrade_errors:
            print("\n  \u274c Default image mismatch after update:")
            for e in upgrade_errors:
                print(e)
            if log:
                log.log(f"Default image mismatch: {upgrade_errors}", prefix="ERROR")
            return False
        print("  \u2705 Default image verified on all nodes.")
        if log:
            log.log("Default image verified on all nodes")

        # ── Step 9: storage failover readiness ──────────────────────────────
        print("\n  \U0001f4e1 Checking storage failover readiness...")
        with _suppress_console():
            out_fo = _run_cluster_command(
                channel_41, "storage failover show", timeout=60
            )
        fo_rows = _parse_failover_show(out_fo)
        if not fo_rows:
            print("  \u274c Could not parse 'storage failover show' output. Exiting.")
            if log:
                log.log("Failed to parse storage failover show", prefix="ERROR")
            return False

        not_ready = [r for r in fo_rows if not r["takeover_possible"]]
        if not_ready:
            print("\n  \u274c Takeover not possible for:")
            for r in not_ready:
                print(f"    {r['node']} (partner: {r['partner']})")
            print("  Resolve failover issues before retrying the upgrade.")
            if log:
                log.log(f"Takeover not possible: {[r['node'] for r in not_ready]}",
                        prefix="ERROR")
            return False
        print("  \u2705 All nodes report takeover-possible=true.")
        if log:
            log.log("All nodes takeover-possible")

        # ── Step 10: build rolling upgrade groups ───────────────────────────
        # Strategy: "partner" group = nodes that will be taken over first;
        # "main" group = nodes that take over.  Each node appears in exactly
        # one group (the partnership set is symmetric, so we pick unique pairs).
        partner_group = []  # taken over first
        main_group    = []  # taken over second
        paired = set()
        for r in fo_rows:
            node    = r["node"]
            partner = r["partner"]
            if node in paired or partner in paired:
                continue
            partner_group.append(partner)
            main_group.append(node)
            paired.add(node)
            paired.add(partner)

        print("\n  Rolling upgrade order:")
        print(f"    Phase 1 (partner group): {partner_group}")
        print(f"    Phase 2 (main group)   : {main_group}")
        if log:
            log.log(f"Upgrade groups — partner: {partner_group}, main: {main_group}")

        # ── Step 11: rolling takeover/giveback ──────────────────────────────
        def _do_takeover_giveback(takeover_node, takeover_by):
            """Take over `takeover_node` by `takeover_by`, wait for
            'waiting for giveback', then give back and wait for reconnect.
            Returns True on success.
            """
            print(f"\n  \U0001f504 Takeover: {takeover_by} takes over {takeover_node}...")
            if log:
                log.log(f"Initiating takeover of {takeover_node} by {takeover_by}")
            with _suppress_console():
                _run_cluster_command(
                    channel_41,
                    f"storage failover takeover -ofnode {takeover_node} "
                    f"-option normal -override-vetoes true",
                    timeout=60,
                )
            print(f"  \u23f3 Waiting for {takeover_node} to reach 'waiting for giveback'...")
            if not _wait_for_failover_state(
                channel_41, takeover_node, "waiting for giveback",
                total_timeout=1800, poll_interval=180, log=log,
                phase_label="takeover/giveback",
            ):
                print(f"  \u274c Timed out waiting for giveback state on {takeover_node}.")
                if log:
                    log.log(f"Timeout waiting for giveback state on {takeover_node}",
                            prefix="ERROR")
                return False
            print(f"  \U0001f501 Giving back {takeover_node}...")
            if log:
                log.log(f"Issuing giveback for {takeover_node}")
            with _suppress_console():
                _run_cluster_command(
                    channel_41,
                    f"storage failover giveback -ofnode {takeover_node} "
                    f"-override-vetoes true",
                    timeout=60,
                )
            print(f"  \u23f3 Waiting for {takeover_node} to reconnect...")
            if not _wait_for_failover_state(
                channel_41, takeover_node, "connected to",
                total_timeout=1800, poll_interval=180, log=log,
                phase_label="node reconnect",
            ):
                print(f"  \u274c Timed out waiting for {takeover_node} to reconnect.")
                if log:
                    log.log(f"Timeout waiting for {takeover_node} reconnect",
                            prefix="ERROR")
                return False
            print(f"  \u2705 {takeover_node} back online.")
            if log:
                log.log(f"{takeover_node} giveback complete and reconnected")
            return True

        # Build a node→its_partner lookup for the takeover calls
        partner_of = {r["node"]: r["partner"] for r in fo_rows}
        partner_of.update({r["partner"]: r["node"] for r in fo_rows})

        for phase, nodes in (("1 (partner)", partner_group), ("2 (main)", main_group)):
            print(f"\n  ── Phase {phase} ─────────────────────────────────────")
            for to_node in nodes:
                by_node = partner_of.get(to_node, "")
                if not _do_takeover_giveback(to_node, by_node):
                    return False

        # ── Step 12: verify version ─────────────────────────────────────────
        print("\n  \U0001f50d Verifying ONTAP version post-upgrade...")
        with _suppress_console():
            out_ver = _run_cluster_command(channel_41, "version", timeout=30)
            out_img2 = _run_cluster_command(
                channel_41,
                "set advanced -c off; system image show -fields version",
                timeout=60,
            )
        # Extract the version string from the 'version' command output
        ver_match = re.search(r"NetApp Release\s+([\d\.]+[^\s:;]+)", out_ver, re.IGNORECASE)
        running_ver = ver_match.group(1).strip() if ver_match else None

        # Extract version from image show output for the non-current (newly installed) images
        img_ver_lines = [l.strip() for l in out_img2.splitlines()
                         if l.strip() and "::" not in l
                         and not l.strip().lower().startswith("system image")]
        print(f"\n  Running version  : {running_ver or '(parse failed)'}")
        print("  Image show output:\n    " + "\n    ".join(img_ver_lines[-10:]))

        if running_ver:
            # Check that every non-blank version token in image show contains
            # the running version (or vice-versa).
            mismatches = []
            for line in img_ver_lines:
                tokens = line.split()
                for tok in tokens:
                    if re.match(r"^\d+\.\d+", tok) and running_ver not in tok and tok not in running_ver:
                        mismatches.append(f"{tok} (line: {line})")
            if mismatches:
                print(f"\n  \u26a0\ufe0f  Version mismatch detected. The upgrade may not have "
                      f"completed correctly.\n"
                      f"     Running : {running_ver}\n"
                      f"     Packages: {mismatches[:5]}\n"
                      f"  Please verify manually or re-run the upgrade.")
                if log:
                    log.log(f"Version mismatch: running={running_ver}, "
                            f"pkg_versions={mismatches[:5]}", prefix="WARN")
            else:
                print(f"\n  \u2705 Version verified: {running_ver}")
                if log:
                    log.log(f"Version verified: {running_ver}")
        else:
            print("  \u26a0\ufe0f  Could not parse running version from 'version' command.")
            if log:
                log.log("Could not parse version output", prefix="WARN")

        if log:
            log.end_phase()

        try:
            channel_41.close()
        except Exception:
            pass
        try:
            client_41.close()
        except Exception:
            pass

        print("\n  \u2705 ONTAP upgrade workflow complete.")
        if log:
            log.log("ONTAP upgrade workflow complete")
        return True

    finally:
        if httpd is not None:
            print("\n  \U0001f310 Shutting down temporary HTTP server...")
            try:
                httpd.shutdown()
            except Exception:
                pass
            if log:
                log.log("Temporary HTTP server stopped")


def _setup_ssh_publickey(channel, mgmt_ip, ssh_user="admin"):
    """Configure passwordless SSH for `ssh_user` on the cluster at `mgmt_ip`.

    Called from within an active cluster-shell session (`channel`). Generates
    a local RSA key pair if absent, installs it on the cluster, then verifies
    the key-based login works.
    """
    import pathlib

    _print_banner("\U0001f511 Configuring passwordless SSH")
    _slog(f"Setting up passwordless SSH for {ssh_user}@{mgmt_ip}")

    # 1. Remove stale known_hosts entry.
    try:
        subprocess.run(["ssh-keygen", "-R", mgmt_ip],
                       check=False, capture_output=True)
        print(f"  🗑️  Removed existing known_hosts entries for {mgmt_ip}.")
        _slog(f"Removed known_hosts entries for {mgmt_ip}")
    except FileNotFoundError:
        print("  ℹ️  ssh-keygen not found; skipping known_hosts cleanup.")
        _slog("ssh-keygen not found; known_hosts cleanup skipped", prefix="WARN")

    # 2. Generate key pair if absent.
    id_rsa = pathlib.Path.home() / ".ssh" / "id_rsa"
    id_rsa_pub = pathlib.Path.home() / ".ssh" / "id_rsa.pub"
    if not id_rsa.exists():
        print("  \U0001f511 Generating RSA-4096 key pair...")
        (pathlib.Path.home() / ".ssh").mkdir(mode=0o700, parents=True, exist_ok=True)
        r = subprocess.run(
            ["ssh-keygen", "-t", "rsa", "-b", "4096", "-f", str(id_rsa), "-N", ""],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"  \u274c ssh-keygen failed: {r.stderr}")
            return
        print("  \u2705 Key pair generated.")
    else:
        print(f"  \u2139\ufe0f  Using existing key: {id_rsa}")

    if not id_rsa_pub.exists():
        print(f"  \u274c Public key not found at {id_rsa_pub}. Skipping.")
        return
    pub_key = id_rsa_pub.read_text(encoding="utf-8").strip()

    # 3. Get cluster name from prompt.
    channel.send("\r")
    _po, _ = direct_read_until_any(channel, ["::>", "::*>"], timeout=15)
    cluster_name = ""
    for _pl in reversed((_po or "").splitlines()):
        _pm = re.match(r'^(\S+)::\*?>\s*$', _pl.strip())
        if _pm:
            cluster_name = _pm.group(1)
            break
    if not cluster_name:
        cluster_name = input("  Cluster name (for vserver parameter): ").strip()

    # 4. Ensure ssh/publickey login entry exists.
    show_out = _run_cluster_command(
        channel,
        f"security login show {ssh_user} -application ssh "
        f"-authentication-method publickey",
        timeout=30,
    )
    if "no entries matching" in show_out.lower():
        role = "admin" if ssh_user.lower() == "admin" else "vsadmin"
        _run_cluster_command(
            channel,
            f"security login create -user-or-group-name {ssh_user} "
            f"-application ssh -authentication-method publickey "
            f"-role {role} -vserver {cluster_name}",
            timeout=30,
        )
        print(f"  \u2705 SSH login entry created for '{ssh_user}'.")

    # 5. Install public key.
    _run_cluster_command(
        channel,
        f'security login publickey create -vserver {cluster_name} '
        f'-username {ssh_user} -publickey "{pub_key}"',
        timeout=30,
    )
    print("  \u2705 Public key installed on cluster.")

    # 6. Test login from this host — open an interactive shell and look for ::>
    print(f"\n  \U0001f50e Testing ssh {ssh_user}@{mgmt_ip}...")
    try:
        _pk_path = os.path.expanduser("~/.ssh/id_rsa")
        _tc = paramiko.SSHClient()
        _tc.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        _tc.connect(
            mgmt_ip, username=ssh_user,
            key_filename=_pk_path,
            look_for_keys=True, allow_agent=False,
            timeout=20,
        )
        _tch = _tc.invoke_shell(width=200, height=50)
        _tout, _tmatch = direct_read_until_any(
            _tch, ["::>", r"::\*>", "password:", "Password:"], timeout=30
        )
        try:
            _tch.close()
        except Exception:
            pass
        _tc.close()
        if "::" in _tmatch:
            print("  \u2705 Passwordless login configuration complete!")
            if _session_log:
                _session_log.log(
                    f"Passwordless SSH verified: {ssh_user}@{mgmt_ip}"
                )
        else:
            print(
                f"  \u26a0\ufe0f  Cluster prompted for a password — "
                f"key may need a moment to activate.\n"
                f"     Test manually with: ssh {ssh_user}@{mgmt_ip}"
            )
            if _session_log:
                _session_log.log(
                    "SSH test: password prompt appeared", prefix="WARN"
                )
    except paramiko.AuthenticationException:
        print(
            f"  \u26a0\ufe0f  Authentication failed — public key not accepted yet.\n"
            f"     Test manually with: ssh {ssh_user}@{mgmt_ip}"
        )
        _slog("SSH test: AuthenticationException", prefix="WARN")
    except Exception as _te:
        print(
            f"  \u26a0\ufe0f  SSH test failed: {_te}\n"
            f"     Test manually with: ssh {ssh_user}@{mgmt_ip}"
        )
        _slog(f"SSH test exception: {_te}", prefix="WARN")


def _run_cluster_setup_wizard(channel):
    """Drive the post-node-mgmt cluster setup wizard non-interactively using
    values gathered in `_cluster_config`.

    Returns True on success, False on fatal failure (caller should exit).
    """
    global _operation_mode, _auto_add

    # If _cluster_config was never populated (e.g. called from the 4b reinit
    # path which only runs _discover_and_prompt_config, not collect_cluster_config),
    # try to build it now from the already-loaded _config_data.  If that also
    # fails, offer the operator another chance to supply a config file before
    # falling back to fully interactive prompts.
    if not _cluster_config:
        print("\n⚠️  Cluster setup config not yet collected.")
        if _session_log:
            _session_log.log(
                "Cluster wizard: _cluster_config empty; attempting to populate",
                prefix="WARN",
            )

        # If _config_data doesn't have a 'cluster' section, re-run discovery
        # so the operator can point at the right file.
        if not (isinstance(_config_data, dict) and _config_data.get("cluster")):
            print("  Config file not loaded or has no 'cluster' section.")
            print("  Searching for config files again...")
            _discover_and_prompt_config(_run_context)

        # Now try to collect from whatever is in _config_data (or prompt).
        print("  Collecting cluster setup values...")
        collect_cluster_config()

    if not _cluster_config:
        msg = "Cluster setup config unavailable after all attempts – cannot drive wizard."
        print(f"\n❌ {msg}")
        if _session_log:
            _session_log.log(msg, prefix="ERROR")
            _session_log.set_outcome("FAIL", msg)
        return False

    cc = _cluster_config

    print("\n🤖 Driving ONTAP cluster setup wizard from collected values...")
    if _session_log:
        _session_log.start_phase("Cluster Setup Wizard (1b)")
        loggable = {k: ("<hidden>" if k == "admin_password" else v) for k, v in cc.items()}
        _session_log.log(f"Wizard inputs: {loggable}")

    # Some ONTAP builds show "Press Enter to complete cluster setup" first;
    # others jump directly to the create/join question. Wait for whichever
    # comes first, sending CR every 15 s of silence to nudge the prompt.
    print("\n⏳ Waiting for cluster setup wizard to begin...")
    _slog("Waiting for wizard start (press-enter or create/join prompt)")
    _which = _wait_for_wizard_start(channel, timeout=1800)
    if _which is None:
        print("\n❌ Timed out waiting for cluster setup wizard start.")
        if _session_log:
            _session_log.log("Timeout waiting for wizard start", prefix="ERROR")
            _session_log.set_outcome("FAIL", "wizard start timeout")
        return False
    if "press enter" in _which.lower():
        print("\n✅ 'Press Enter' prompt detected – sending Enter")
        _slog("Sent Enter at 'Press Enter to complete cluster setup'")
        channel.send("\r")
        time.sleep(0.5)
        _wait_and_send(channel, "do you want to create a new cluster or join", "create",
                       "Create or join cluster -> create", timeout=600)
    else:
        print("\n✅ Create/join prompt detected (no 'Press Enter' screen) – sending 'create'")
        _slog("Sent 'create' at create/join prompt (Press Enter screen skipped)")
        channel.send("create\r")
        if _session_log:
            _session_log.log_sent("create")
        time.sleep(0.5)
    _wait_and_send(channel, "{yes, no}", "yes",
                   "Yes/no confirmation after create -> yes", timeout=600)
    _wait_and_send(channel, "enter the cluster administrator", cc["admin_password"],
                   "Cluster administrator password", timeout=600, hide_in_log=True)
    _wait_and_send(channel, "retype the password", cc["admin_password"],
                   "Retype cluster administrator password", timeout=600, hide_in_log=True)
    _wait_and_send(channel, "enter the cluster name", cc["name"],
                   f"Cluster name -> {cc['name']}", timeout=600)

    # After cluster creation:
    print("\n⏳ Cluster creating...", end="", flush=True)
    _slog("Cluster creating – waiting for license key prompt")
    _dot_done = threading.Event()
    def _dot_ticker(_ev=_dot_done):
        while not _ev.wait(15):
            sys.stdout.write(".")
            sys.stdout.flush()
    threading.Thread(target=_dot_ticker, daemon=True).start()
    _wait_and_send(channel, "enter an additional license key", "",
                   "Additional license key -> Enter", timeout=1800, quiet=True)
    _dot_done.set()
    print()  # newline after dots
    _log_path = _session_log.log_file if _session_log else "the log file"
    print(f"\n⏳ Cluster creating. See log for details in a separate SSH session:\n   {_log_path}")
    _wait_and_send(channel, "cluster management interface port", cc["mgmt_port"],
                   f"Cluster mgmt port -> {cc['mgmt_port']}", timeout=900)
    _wait_and_send(channel, "cluster management interface ip address", cc["mgmt_ip"],
                   f"Cluster mgmt IP -> {cc['mgmt_ip']}", timeout=600)
    _wait_and_send(channel, "cluster management interface netmask", cc["mgmt_netmask"],
                   f"Cluster mgmt netmask -> {cc['mgmt_netmask']}", timeout=600)
    # Send the cluster-management gateway and re-prompt on rejection.
    # ONTAP prints "not a valid gateway address" when the supplied value is
    # outside the management interface's subnet.
    _gw_to_send = cc.get("mgmt_gateway") or ""
    while True:
        print("\n⏳ Waiting for: Cluster mgmt gateway...")
        _slog("Waiting for: cluster management interface default gateway")
        direct_send_and_wait(
            channel, "", "cluster management interface default gateway",
            timeout=600, check_bmc_drop=True,
        )
        channel.send(_gw_to_send + "\r")
        if _session_log:
            _session_log.log_sent(_gw_to_send if _gw_to_send else "<Enter>")
        time.sleep(0.5)
        # Read briefly to detect ONTAP gateway-validation errors.
        _gw_recheck = ""
        _gw_rc_start = time.monotonic()
        while time.monotonic() - _gw_rc_start < 4:
            if channel.recv_ready():
                _gc = channel.recv(4096).decode("utf-8", errors="replace")
                _gw_recheck += _gc
                sys.stdout.write(_gc)
                sys.stdout.flush()
                if _session_log:
                    _session_log.log_console(_gc)
            else:
                time.sleep(0.1)
        if "not a valid gateway" in _gw_recheck.lower():
            print(
                f"\n  ❌ Gateway '{_gw_to_send}' rejected by ONTAP "
                "('not a valid gateway address')."
            )
            if _session_log:
                _session_log.log(
                    f"Gateway '{_gw_to_send}' rejected: 'not a valid gateway address'",
                    prefix="WARN",
                )
            _gw_to_send = _prompt(
                "  Enter a valid cluster management gateway: "
            )
            # Update cc so the corrected value is persisted in _cluster_config.
            cc["mgmt_gateway"] = _gw_to_send
            _cluster_config["mgmt_gateway"] = _gw_to_send
            # Loop back: ONTAP will re-prompt after the bad value.
            continue
        break  # value accepted — move on
    # The 4-second gateway-validation drain may have already consumed the DNS /
    # name-server / location prompts when ONTAP accepts the gateway quickly.
    # For each step, if the trigger was already seen in _gw_recheck, send the
    # answer immediately instead of calling _wait_and_send (which would hang
    # waiting for text that was already read and discarded).
    def _wizard_step(trigger, response, label, timeout=600, hide_in_log=False):
        if trigger in _gw_recheck.lower():
            print(f"\n✅ Prompt already received (fast cluster): {label}")
            _slog(f"{label}: prompt already seen in gateway drain; sending directly")
            channel.send((response or "") + "\r")
            if _session_log:
                if hide_in_log:
                    _session_log.log(f"[{label}] sent <hidden>")
                else:
                    _session_log.log_sent(response if response else "<Enter>")
            time.sleep(0.5)
        else:
            _wait_and_send(channel, trigger, response, label,
                           timeout=timeout, hide_in_log=hide_in_log)
    _wizard_step("dns domain name", cc["dns_domains"] or "",
                 f"DNS domain names -> {cc['dns_domains']}")
    _wizard_step("name server ip address", cc["dns_servers"] or "",
                 f"DNS servers -> {cc['dns_servers']}")
    _wizard_step("where is the controller located", cc["location"] or "",
                 f"Controller location -> {cc['location']}")

    # Watch the post-wizard console output for milestone log lines and
    # print friendly status updates before waiting for login:.
    _log_path = _session_log.log_file if _session_log else "the log file"
    print(f"\n⏳ Configuring cluster. For details see log in a separate SSH session:\n   {_log_path}")
    _slog("Monitoring post-wizard output for cluster creation milestones")
    _saz_done = False
    _cluster_created = False
    _create_deadline = time.monotonic() + 1800
    while time.monotonic() < _create_deadline:
        _remaining_ms = max(10, int(_create_deadline - time.monotonic()))
        _out, _matched = direct_read_until_any(
            channel,
            ["creating root aggregate", "has been created", "login:"],
            timeout=_remaining_ms,
            check_bmc_drop=True,
        )
        if not _matched:
            break  # timeout – fall through to login: wait below
        _ml = _matched.lower()
        if "creating root aggregate" in _ml and not _saz_done:
            _saz_done = True
            print("\n✅ Storage Availability Zone successfully created. Configuring capacity pool.")
            _slog("Milestone: root aggregate creation started (SAZ created)")
            continue
        if "has been created" in _ml and not _cluster_created:
            # Extract cluster name from the matched output line.
            _cname = cc.get("name", "")
            for _line in (_out + _matched).splitlines():
                _m = re.search(r'[Cc]luster\s+(\S+)\s+has been created', _line)
                if _m:
                    _cname = _m.group(1)
                    break
            _cluster_created = True
            print(f"\n✅ Cluster {_cname} has been created.")
            print("\n⏳ Configuring cluster.")
            _slog(f"Milestone: cluster '{_cname}' has been created")
            continue
        if "login:" in _ml:
            break  # cluster creation fully complete

    # If we exited the loop without seeing login: yet, wait for it now.
    if not _matched or "login:" not in _matched.lower():
        _slog("Waiting for login: prompt to confirm cluster creation")
        direct_send_and_wait(channel, "", "login:", timeout=1800)

    print("\n✅ Cluster creation complete.")
    if _session_log:
        _session_log.log("Cluster creation complete (login: prompt observed)")
        _session_log.end_phase()

    # Checkpoint: cluster create has succeeded and the primary is at the
    # ONTAP login prompt. Mode 4b's standalone wrapper writes this same
    # marker at its own completion point; writing it here covers the
    # direct mode-1b / mode-3 entry path that uses _run_cluster_setup_wizard.
    if _checkpoint:
        try:
            _checkpoint.mark_done("cluster_formed")
            _slog("checkpoint: cluster_formed saved")
        except Exception:
            pass

    # Apply any pre-configured ONTAP license(s).
    if _license_mode:
        if _login_primary_cluster_shell(channel, cc.get("admin_password")):
            _apply_license(channel)
        else:
            print(
                "\\n  Warning: Could not log in to cluster shell for "
                "license application."
            )
            if _session_log:
                _session_log.log(
                    "Cluster shell login failed for license application",
                    prefix="WARN",
                )

    # Set up passwordless SSH if the operator requested it during 1a/1b.
    if _setup_passwordless_ssh:
        _ssh_mgmt_ip = cc.get("mgmt_ip") or ""
        if not _ssh_mgmt_ip:
            _ssh_mgmt_ip = input(
                "  Cluster management IP for SSH setup: "
            ).strip()
        if _ssh_mgmt_ip:
            # Ensure we're logged in to the cluster shell before calling.
            if not _login_primary_cluster_shell(channel, cc.get("admin_password")):
                print("  \u26a0\ufe0f  Could not log in for SSH setup; skipping.")
            else:
                _setup_ssh_publickey(channel, _ssh_mgmt_ip, ssh_user="admin")

    # Mode 3: launch parallel auto-add for every peer BMC.
    if _operation_mode == 3 and _peer_bmc_list:
        add_peer_nodes_parallel(channel, _peer_bmc_list, cc.get("admin_password"))
        _option3_finalize(_run_context, cc.get("mgmt_ip"))

    # Ask the operator whether to continue (e.g. into interactive add-node
    # flow) or stop the script with a friendly summary.
    # For automated modes (1b, 2b, 3) answer yes automatically.
    if _auto_setup or _auto_add or _operation_mode == 3:
        ans = "y"
        print("\n✅ Cluster creation complete. Continuing to add nodes... [auto-answered]")
        _slog("Continue to add nodes? y [auto-answered for automated mode]")
    else:
        try:
            print("  " + "─" * 58)
            ans = input(
                "\nCluster creation complete. Would you like to continue the "
                "script to add nodes? (y/N): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if _session_log:
            _session_log.log_user_input(f"Continue to add nodes? {ans}")

    if ans != "y":
        mgmt_ip = cc.get("mgmt_ip") or "<cluster-management-ip>"
        _print_banner("✅ Configuration complete.")
        print(f"  To login to the cluster, SSH to {mgmt_ip} or use a web")
        print(f"  browser to access https://{mgmt_ip}")
        print("=" * 60)
        if _session_log:
            _session_log.log("User declined to continue; exiting cleanly")
            _session_log.log(f"SSH to {mgmt_ip} or https://{mgmt_ip}")
            _session_log.set_outcome("PASS", "cluster setup complete; user chose not to add nodes")
            try:
                _session_log.close()
            except Exception:
                pass
        sys.exit(0)

    # Ask whether to add nodes interactively (2a) or automatically (2b).
    # Mode 3 always auto-selects 2b (no prompt needed).
    if _operation_mode == 3:
        node_choice = "2b"
        print("\n✅ Mode 3: auto-selecting 2b (automatic node add).")
        _slog("Mode 3: auto-selected 2b for node add")
    else:
        _print_banner("➕ Add Nodes")
        print("")
        print("  2a. Add nodes interactively  (manual prompts at each step)")
        print("  2b. Add nodes automatically  (auto-answer all prompts)")
        print("")
        while True:
            try:
                print("  " + "─" * 58)
                node_choice = input(
                    "  Enter sub-option (2a / 2b) or blank to skip: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                node_choice = ""
            if node_choice == "":
                print("\n  Skipping node add.")
                _slog("User skipped node add after cluster creation")
                return
            if node_choice in ("2a", "2b"):
                break
            print("  ⚠️  Please enter 2a or 2b.")

    _operation_mode = 2
    _auto_add = (node_choice == "2b")
    print(
        f"\n  \u2705 Confirmed. {node_choice.upper()}: "
        f"{'Automatic' if _auto_add else 'Interactive'} node add.\n"
    )
    if _peer_log_paths:
        print("  Console log for node at:\n")
        for _wn_idx, (_wn_ip, _wn_path) in enumerate(_peer_log_paths.items(), 1):
            print(f"    Node being added BMC{_wn_idx} ({_wn_ip}): {_wn_path}")
        print("")
    if _session_log:
        _session_log.log(
            f"User selected {node_choice} for node add after cluster creation"
        )

    print("\n\u27a1\ufe0f  Continuing into node-add session...")
    _slog("Transitioning to node-add flow")


# ---------------------------------------------------------------------------
# Mode 2b / Mode 3 peer: auto join wizard
# ---------------------------------------------------------------------------

def _run_join_wizard(channel, label="join wizard"):
    """Drive the post-option-4 setup wizard to JOIN an existing cluster.

    Assumes node-management config has already been answered. Acquires
    `_join_lock` around the create/join answer so parallel peer-add threads
    don't collide. Returns True on success, False on timeout/abort.
    """
    print(f"\n🤖 [{label}] Driving join wizard...")
    _slog(f"[{label}] starting join wizard automation")

    # Some ONTAP builds show "Press Enter to complete cluster setup" first;
    # others jump directly to the create/join question. Wait for whichever
    # comes first, sending CR every 15 s of silence to nudge the prompt.
    print(f"\n⏳ [{label}] Waiting for cluster setup wizard to begin...")
    _slog(f"[{label}] waiting for wizard start (press-enter or create/join prompt)")
    _which = _wait_for_wizard_start(channel, timeout=1800)
    if _which is None:
        print(f"\n❌ [{label}] Timed out waiting for cluster setup wizard start.")
        _slog(f"[{label}] Timeout waiting for wizard start", prefix="ERROR")
        return False
    if "press enter" in _which.lower():
        print(f"\n✅ [{label}] 'Press Enter' prompt detected – sending Enter")
        _slog(f"[{label}] Sent Enter at 'Press Enter to complete cluster setup'")
        channel.send("\r")
        time.sleep(0.5)
        # Serialize the join keystroke across peer-add threads.
        print(f"\n🔒 [{label}] Waiting for join lock...")
        _slog(f"[{label}] waiting to acquire _join_lock")
        with _join_lock:
            _slog(f"[{label}] acquired _join_lock; sending 'join'")
            _wait_and_send(channel, "do you want to create a new cluster or join",
                           "join", f"[{label}] Create or join -> join", timeout=900)
    else:
        print(f"\n✅ [{label}] Create/join prompt detected (no 'Press Enter' screen) – sending 'join'")
        _slog(f"[{label}] Sent 'join' at create/join prompt (Press Enter screen skipped)")
        print(f"\n🔒 [{label}] Waiting for join lock...")
        _slog(f"[{label}] waiting to acquire _join_lock")
        with _join_lock:
            _slog(f"[{label}] acquired _join_lock; sending 'join'")
            channel.send("join\r")
            if _session_log:
                _session_log.log_sent("join")
            time.sleep(0.5)
        # Some builds re-prompt for confirmation.
        # We don't wait for cluster_show here when run from the primary
        # channel (mode 2b single add); verification is the caller's choice.
    return True


def auto_complete_join(channel, client, sp_host, sp_user, sp_pass, bmc_host=None,
                       no_add_another=False):
    """Mode 2b: auto-drive the post-option-4 prompts to add this node to an
    existing cluster. Runs on the primary BMC channel. Drives every prompt
    through the final `login:` line, then asks the operator whether to add
    another node.
    """
    global _add_another_node_request
    print("\n🤖 Mode 2b: automated node-add in progress...")
    if _session_log:
        _session_log.start_phase("Auto Join (2b)")
        _session_log.log("Mode 2b automated join starting after option 4 sent")

    # Yes confirmations after option 4.
    _auto_answer_disk_erase_prompts(channel, label=bmc_host or sp_host or "",
                                    is_node_add=True)

    # Node mgmt config (from per-BMC pre-collection).
    cfg = _resolve_node_mgmt_config(bmc_host)
    print("\n📋 Node management config to apply:")
    for k in ("port", "ip", "netmask", "gateway"):
        v = cfg.get(k)
        print(f"   {k:<8} = {v if v else '(prompt manually)'}")
    _slog(f"Node mgmt config to use: {cfg}")
    _auto_answer_node_mgmt(channel, cfg)

    # Drive the join wizard (sends "join" at create-or-join prompt).
    _run_join_wizard(channel, label=f"2b/{bmc_host or 'this node'}")

    # ---- Post-create/join: drive every remaining prompt through "login:".
    # 1. Confirm "use this configuration?" with yes.
    print("\n⏳ Auto-confirming 'use this configuration?'...")
    _slog("Waiting for join confirmation prompt")
    direct_send_and_wait(channel, "", "[yes]:", timeout=900, auto_respond="yes")

    # 2. Cluster-network IP (looked up from existing cluster).
    print("\n📡 Looking up a cluster-network IP from the existing cluster...")
    _slog("Looking up cluster-network IP")
    cluster_iface_ip = _fetch_existing_cluster_ip(
        bmc_user=sp_user, bmc_password=sp_pass,
    )

    print("\n⏳ Waiting for cluster-network IP prompt...")
    direct_send_and_wait(
        channel, "", "enter the ip address of an interface on the private",
        timeout=900,
    )
    if not cluster_iface_ip:
        print("\n⚠️  No cluster-network IP available; falling back to interactive entry.")
        if _session_log:
            _session_log.log("Cluster IP unavailable; falling back to interactive",
                             prefix="WARN")
        if _session_log:
            _session_log.end_phase()
        return
    print(f"\n✅ Sending cluster-network IP: {cluster_iface_ip}")
    channel.send(cluster_iface_ip + "\r")
    if _session_log:
        _session_log.log_sent(cluster_iface_ip)
    time.sleep(0.5)

    # 3. Username (use the BMC username).
    print("\n⏳ Waiting for username prompt...")
    direct_send_and_wait(channel, "", "username", timeout=600)
    print(f"\n✅ Sending BMC username: {sp_user}")
    channel.send(sp_user + "\r")
    if _session_log:
        _session_log.log_sent(sp_user)
    time.sleep(0.5)

    # 4. Password – the prompt is asking for the cluster admin password, not
    #    the BMC password.  Prefer the stored cluster admin password; fall
    #    back to the BMC password only if nothing better is available.
    _join_pw = (_cluster_config.get("admin_password")
                or sp_pass
                or "")
    print("\n⏳ Waiting for password prompt...")
    direct_send_and_wait(channel, "", "password", timeout=600)
    print("\n✅ Sending cluster admin password (hidden).")
    channel.send(_join_pw + "\r")
    if _session_log:
        _pw_src = ("cluster admin" if _cluster_config.get("admin_password") else "BMC")
        _session_log.log(f"Sent {_pw_src} password (<hidden>)")
    time.sleep(0.5)

    # 5. Wait for the login: prompt – marks the join as complete.
    #    ONTAP may also present "Add another node to the cluster? [Y/N]"
    #    before the login prompt; if so, ask the operator and respond.
    _join_node_label = bmc_host or sp_host or "node"
    print(f"\n⏳ [{_join_node_label}] Waiting for 'login:' prompt – node is joining the cluster...")
    _slog("Waiting for 'login:' to confirm node join completed")
    _join_deadline = time.monotonic() + 3600
    while True:
        _remaining = max(1, int(_join_deadline - time.monotonic()))
        _out, _matched = direct_read_until_any(
            channel,
            ["login:", "add another node to the cluster"],
            timeout=_remaining,
            check_bmc_drop=True,
        )
        if not _matched:
            print("\n⚠️  'login:' not seen; you may need to monitor manually.")
            if _session_log:
                _session_log.log("'login:' not observed within timeout", prefix="WARN")
                _session_log.end_phase()
            return
        if "login:" in _matched.lower():
            break
        # ONTAP asked "Add another node to the cluster? [Y/N]" – always
        # answer 'no' here. This function only drives one join; the outer
        # loop below offers add-another via stdin after login: is reached.
        _ontap_ans_str = "no"
        print(f"\n✅ ONTAP: 'Add another node to the cluster?' – auto-answering '{_ontap_ans_str}'")
        channel.send(_ontap_ans_str + "\r")
        if _session_log:
            _session_log.log(f"ONTAP 'add another node?' – auto-answered '{_ontap_ans_str}'")
            _session_log.log_sent(_ontap_ans_str)
    print(f"\n🎉 Node {bmc_host or sp_host} has joined the cluster!")
    if _session_log:
        _session_log.log(f"Node {bmc_host or sp_host} reached login: prompt")
        _session_log.end_phase()

    # 6. Offer to add another node in the same run.
    # Mark the current node as done so it is never offered again.
    _2b_processed_bmcs.add(bmc_host or sp_host)

    if no_add_another:
        # Called from a parallel worker — skip interactive stdin to avoid
        # multiple threads racing on the same input stream.
        return

    while True:
        try:
            _real_stdout.write("\n  " + "─" * 58 + "\n")
            _real_stdout.write("\n➕ Add another node to the cluster? [Y/N]: ")
            _real_stdout.flush()
            ans = sys.stdin.readline().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if _session_log:
            _session_log.log_user_input(f"Add another node? {ans}")
        if ans != "y":
            print("\n👋 No more nodes to add – exiting.")
            _slog("Operator declined to add another node; exiting")
            _shutdown_event.set()
            return

        # ── Check config file for un-processed peer nodes first ───────────
        _cfg_nodes = (_config_data.get("nodes") or []) if isinstance(_config_data, dict) else []
        _cfg_pending = [
            n for n in _cfg_nodes
            if isinstance(n, dict)
            and n.get("bmc")
            and n["bmc"] not in _2b_processed_bmcs
        ]
        if _cfg_pending:
            print(f"\n  📄 Found {len(_cfg_pending)} additional node(s) in the config file:")
            for _cn in _cfg_pending:
                print(f"     {_cn['bmc']}")

        next_host = None
        next_user = sp_user
        next_pass = sp_pass

        for _cfg_node in _cfg_pending:
            _cfg_bmc  = _cfg_node["bmc"]
            _cfg_user = _cfg_node.get("bmc_user") or sp_user
            _cfg_pass = _cfg_node.get("bmc_password")
            if _cfg_pass is None:
                _cfg_pass = sp_pass
            try:
                _real_stdout.write(f"\n  Use BMC '{_cfg_bmc}' from config? [Y/n]: ")
                _real_stdout.flush()
                _use_ans = sys.stdin.readline().strip().lower()
            except (EOFError, KeyboardInterrupt):
                _use_ans = "y"
            if _session_log:
                _session_log.log_user_input(f"Use config BMC {_cfg_bmc}? {_use_ans}")
            if _use_ans in ("", "y", "yes"):
                next_host = _cfg_bmc
                next_user = _cfg_user
                next_pass = _cfg_pass
                print(f"  \u2705 Using config BMC: {_cfg_bmc} (user={_cfg_user})")
                if _session_log:
                    _session_log.log(
                        f"Mode 2b: using config BMC {_cfg_bmc} (user={_cfg_user})"
                    )
                break

        if next_host is None:
            # No config node accepted – fall back to manual entry.
            try:
                _real_stdout.write("  Next BMC IP/hostname: ")
                _real_stdout.flush()
                next_host = sys.stdin.readline().strip()
            except (EOFError, KeyboardInterrupt):
                next_host = ""
            if not next_host:
                _real_stdout.write("  (No host entered; ask again.)\n")
                _real_stdout.flush()
                continue
            try:
                _real_stdout.write(f"  BMC username [{sp_user}]: ")
                _real_stdout.flush()
                _u_in = sys.stdin.readline().strip()
                next_user = _u_in or sp_user
            except (EOFError, KeyboardInterrupt):
                next_user = sp_user
            try:
                next_pass = getpass.getpass(
                    f"  BMC password for {next_user}@{next_host} "
                    f"(blank to reuse current): "
                ) or sp_pass
            except (EOFError, KeyboardInterrupt):
                next_pass = sp_pass

        _add_another_node_request = (next_host, next_user, next_pass)
        if _session_log:
            _session_log.log(
                f"Operator requested another node: {next_host} (user={next_user})"
            )
        # Signal the outer monitor to stop and let main() drive the next node.
        return


# ---------------------------------------------------------------------------
# Mode 3: parallel peer-add orchestration
# ---------------------------------------------------------------------------

def _login_primary_cluster_shell(channel, admin_password):
    """At the post-cluster-create login: prompt, log in as admin so we can
    run `cluster show` to verify peer joins. Returns True on success.
    """
    drain_channel(channel, seconds=0.5)
    channel.send("\r")
    out, matched = direct_read_until_any(
        channel, ["login:", "::>", "::*>"], timeout=20
    )
    if matched and ("::>" in matched or "::*>" in matched):
        return True
    if not matched:
        return False
    _slog("Logging into primary cluster shell as admin (for cluster show)")
    channel.send("admin\r")
    out, matched = direct_read_until_any(channel, ["password:", "login:"], timeout=15)
    if not matched or "login:" in matched.lower():
        return False
    channel.send((admin_password or "") + "\r")
    out, matched = direct_read_until_any(channel, ["::>", "::*>", "login:"], timeout=20)
    return bool(matched and ("::>" in matched or "::*>" in matched))


def _cluster_show_node_count(channel):
    """Run `cluster show` on the primary's cluster shell and return the
    number of node rows. Returns -1 on parse failure.
    """
    rows, _, _ = _cluster_show_node_status(channel)
    return rows


def _cluster_show_node_status(channel):
    """Run `cluster show` and return `(node_rows, all_true, has_warning)`.

    `all_true` is True only if every node row reports both Health and
    Eligibility as 'true'. `has_warning` is True if any line in the
    command output contains the word 'warning' (case-insensitive) — for
    example the post-join 'Cluster HA must be configured...' notice.
    Returns `(-1, False, False)` on parse failure.
    """
    with _primary_shell_lock:
        out = _run_cluster_command(channel, "cluster show", timeout=30)
    rows = 0
    dashes_seen = False
    table_done = False
    all_true = True
    has_warning = "warning" in out.lower()
    for raw_line in out.splitlines():
        s = raw_line.strip()
        # An empty line *after* the dashes terminates the data table —
        # anything that follows (warning paragraphs, "N entries were
        # displayed", new prompt) must NOT be counted as a node row.
        if not s:
            if dashes_seen:
                table_done = True
            continue
        if table_done:
            continue
        if "::" in s or s.lower().startswith("cluster show"):
            continue
        if "entries were displayed" in s.lower():
            table_done = True
            continue
        # Some ONTAP releases emit the warning block without a separating
        # blank line. Treat any "Warning:" prefix (or its wrapped
        # continuation indented well past the table) as end-of-table too.
        if s.lower().startswith("warning"):
            table_done = True
            continue
        if set(s) <= {"-", " "}:
            dashes_seen = True
            continue
        if dashes_seen:
            tokens = s.split()
            if not tokens:
                continue
            rows += 1
            # Expected layout: "<node-name>  <health>  <eligibility>  [epsilon]".
            # Only the first two status columns (health, eligibility) must be
            # "true". Epsilon is managed by cluster HA and may legitimately be
            # "false" after `cluster ha modify`, so it is intentionally ignored.
            statuses = [t.lower() for t in tokens[1:3]]
            if not statuses or any(t != "true" for t in statuses):
                all_true = False
    if not dashes_seen:
        return -1, False, has_warning
    return rows, all_true, has_warning


def _wait_for_cluster_nodes_healthy(channel, target_count, total_timeout=600,
                                    poll_interval=120, label="",
                                    final_count=None):
    """Poll `cluster show` until it reports `target_count` nodes, every
    node row shows 'true' for Health and Eligibility, AND no 'warning'
    text appears in the command output (e.g. the post-join 'Cluster HA
    must be configured' notice).

    Defaults: up to ``total_timeout`` seconds (10 minutes) of polling at
    ``poll_interval`` second intervals (120s). Retry details are written
    to the log only; the console shows only the initial wait message and
    the final outcome.
    *final_count* is the expected cluster size once ALL peers have joined;
    cluster HA modify is attempted only when final_count == 2.  Pass None
    to fall back to target_count (single-node-at-a-time callers).
    Returns True on success, False on timeout / shutdown.
    When *channel* is None (no primary cluster-shell available), the
    verification step is skipped and True is returned immediately.
    """
    if channel is None:
        return True  # no primary channel — skip cluster-show verification
    prefix = f"[{label}] " if label else ""
    attempt = 0
    ha_fix_attempted = False
    ha_attempt_count = 0
    start = time.monotonic()
    while True:
        if _shutdown_event.is_set():
            return False
        attempt += 1
        try:
            count, all_true, has_warning = _cluster_show_node_status(channel)
        except Exception as e:
            if _session_log:
                _session_log.log(
                    f"{prefix}cluster show poll error: {e}", prefix="WARN"
                )
            count, all_true, has_warning = -1, False, False
        elapsed = time.monotonic() - start
        if count >= target_count and all_true and not has_warning:
            print(f"\n   ✅ {prefix}cluster show reports {count} healthy node(s) "
                  f"after {elapsed:.0f}s.")
            if _session_log:
                _session_log.log(
                    f"{prefix}cluster show healthy: {count}/{target_count} "
                    f"nodes after {elapsed:.0f}s (attempt {attempt})"
                )
            return True
        # Still waiting. Decide whether to retry or give up.
        if elapsed + poll_interval > total_timeout:
            print(f"\n   ⚠️  {prefix}cluster show did not reach {target_count} "
                  f"healthy node(s) within {total_timeout}s "
                  f"(last: count={count}, all_true={all_true}, "
                  f"has_warning={has_warning}).")
            if _session_log:
                _session_log.log(
                    f"{prefix}cluster show timeout after {elapsed:.0f}s "
                    f"(count={count}, all_true={all_true}, "
                    f"has_warning={has_warning})",
                    prefix="WARN",
                )
            # If we tried to fix the HA warning one or more times and the
            # warning still hasn't cleared, surface a guidance message so
            # the operator knows what to do next.
            if ha_attempt_count > 0 and has_warning:
                msg = ("Cluster HA modify failed. Try again manually later. "
                       "If the issue persists, contact NetApp support.")
                print(f"\n   ❌ {prefix}{msg}")
                _slog(f"{prefix}{msg}", prefix="ERROR")
            return False
        if _session_log:
            _session_log.log(
                f"{prefix}cluster show retry: count={count}, "
                f"all_true={all_true}, has_warning={has_warning}; "
                f"sleeping {poll_interval}s "
                f"(attempt {attempt}, elapsed {elapsed:.0f}s)"
            )
        # If cluster show is otherwise healthy but only the cluster-HA
        # warning remains, run the documented fix only for 2-node clusters:
        #   `cluster ha modify -configured true`
        # For clusters with >2 nodes, cluster HA is already configured
        # false (set when the third node was first added), so no modify
        # is needed — we just wait for the warning to clear on its own.
        # Auto-answer y to the y/n confirmation.
        # If ONTAP rejects the modify with an error, run `cluster ha show`
        # to see whether HA is already in the desired state — when it is,
        # we're done; when it isn't, leave the attempt-flag clear so the
        # next poll iteration tries the modify again.
        _ha_target = final_count if final_count is not None else target_count
        if (not ha_fix_attempted and has_warning
                and count >= target_count and all_true
                and _ha_target == 2):
            ha_attempt_count += 1
            ha_cmd = "cluster ha modify -configured true"
            if _session_log:
                _session_log.log(f"{prefix}running '{ha_cmd}' to clear "
                                 f"HA warning (final_count={_ha_target})")
            ha_output = ""
            try:
                with _primary_shell_lock:
                    # ONTAP cluster ha modify uses {y|n} (pipe, not slash).
                    # We match on "y|n" which is a substring of both
                    # "{y|n}" and "(y|n)" to handle all prompt variants.
                    ha_output = direct_send_and_wait(
                        channel, ha_cmd,
                        "y|n", timeout=15, auto_respond="y",
                    ) or ""
                    # Drain the rest of the command output up to the
                    # next cluster prompt so the next cluster show is
                    # clean.
                    ha_output += _run_cluster_command(
                        channel, "", timeout=30
                    ) or ""
            except Exception as e:
                if _session_log:
                    _session_log.log(
                        f"{prefix}cluster ha modify failed: {e}",
                        prefix="WARN",
                    )
                ha_output = ""

            # Inspect the modify output for "error". If found, query
            # `cluster ha show` to learn the actual configured state.
            if ha_output and "error" in ha_output.lower():
                if _session_log:
                    _session_log.log(
                        f"{prefix}cluster ha modify returned error; "
                        "running 'cluster ha show' to verify state",
                        prefix="WARN",
                    )
                try:
                    with _primary_shell_lock:
                        ha_show_out = _run_cluster_command(
                            channel, "cluster ha show", timeout=30
                        ) or ""
                except Exception as e:
                    ha_show_out = ""
                    if _session_log:
                        _session_log.log(
                            f"{prefix}cluster ha show failed: {e}",
                            prefix="WARN",
                        )
                # Parse "High-Availability Configured: true|false"
                # (case-insensitive). Anything else => unknown; treat as
                # not-configured so we retry next loop iteration.
                ha_show_lower = ha_show_out.lower()
                ha_is_true = (
                    "high-availability configured: true" in ha_show_lower
                )
                ha_is_false = (
                    "high-availability configured: false" in ha_show_lower
                )
                if ha_is_true:
                    print(f"\n   ✅ {prefix}'cluster ha show' reports HA "
                          "already configured; nothing more to do.")
                    if _session_log:
                        _session_log.log(
                            f"{prefix}cluster ha show: Configured=true; "
                            "treating modify-error as benign"
                        )
                    ha_fix_attempted = True
                else:
                    state = "false" if ha_is_false else "unknown"
                    if _session_log:
                        _session_log.log(
                            f"{prefix}cluster ha show: Configured={state}; "
                            "scheduling cluster ha modify retry",
                            prefix="WARN",
                        )
                    # Leave ha_fix_attempted False so the next iteration
                    # tries again.
            else:
                ha_fix_attempted = True
                _slog(f"{prefix}cluster ha modify completed")
        # Sleep with shutdown sensitivity so Ctrl-C aborts promptly.
        slept = 0.0
        while slept < poll_interval:
            if _shutdown_event.is_set():
                return False
            time.sleep(min(0.5, poll_interval - slept))
            slept += 0.5


def _add_peer_node_thread(peer_bmc, peer_user, peer_password, primary_channel,
                          admin_password, expected_count_after,
                          join_barrier=None, join_proceed_events=None,
                          join_index=0, final_cluster_count=0,
                          timings_record=None, barrier_release_box=None,
                          timings_lock=None, first_join_sent_box=None):
    """Run a full add-node automation against a single peer BMC.

    All peer threads run LOADER/format in parallel. When each thread
    finishes setup it waits at `join_barrier` until every node is ready,
    then joins in `peer_bmcs` list order (index 0 first) via the
    `join_proceed_events` chain.

    If ``timings_record`` is provided, this thread records its split
    timing (option-4/format prep vs. node-join wall time) into
    ``timings_record[peer_bmc]`` under ``timings_lock`` so the caller
    can render a per-node breakdown in the session summary. The first
    thread to clear ``join_barrier`` also stamps ``barrier_release_box[0]``
    with the shared barrier-release timestamp. The first thread to send
    "join" stamps ``first_join_sent_box[0]`` so the caller can compute
    the wall-clock time from the first cluster-join send to the last
    node's health confirmation.
    """
    label = f"peer/{peer_bmc}"
    print(f"\n🧵 [{label}] Starting peer auto-add thread...")
    if _session_log:
        _session_log.log(f"[{label}] thread starting (expected count after = "
                         f"{expected_count_after})")

    # Per-thread timing anchors. Using monotonic so the deltas are not
    # affected by wall-clock adjustments.
    _t_thread_start = time.monotonic()
    _t_barrier_pass = None

    # Open per-node log file.
    _nf_log_dir = (_session_log.log_dir
                   if _session_log and hasattr(_session_log, 'log_dir')
                   else os.getcwd())
    node_file = None
    try:
        node_file = _node_log_open(peer_bmc, _nf_log_dir, prefix="2b_node")
        print(f"   📝 [{label}] Log: {node_file.name}")
        _slog(f"[{label}] node log: {node_file.name}")
    except Exception as _nfe:
        print(f"   ⚠️  [{label}] Could not open node log: {_nfe}")

    client = None
    ch = None
    _passed_barrier = False
    try:
        try:
            client, peer_user, peer_password = _ssh_connect_with_retry(
                peer_bmc, peer_user, peer_password,
                label=label, max_attempts=5, interactive=True,
            )
        except Exception as e:
            print(f"   ❌ [{label}] could not authenticate: {e}")
            if _session_log:
                _session_log.log(f"[{label}] auth/connect failed: {e}",
                                 prefix="ERROR")
            return False
        # Persist any updated credentials so subsequent steps reuse them.
        _peer_bmc_creds[peer_bmc] = {"user": peer_user, "password": peer_password}
        ch = _open_shell(client)

        # BMC takeover – accept if another session is active.
        if not _reach_bmc_prompt(ch, node_log=node_file):
            print(f"   ⚠️  [{label}] no BMC prompt; aborting.")
            return False

        # Reset the node to begin a clean boot cycle.
        print(f"   🔄 [{label}] Sending system reset...")
        _slog(f"[{label}] sending system reset")
        ch.send("system reset\r")
        out, matched = direct_read_until_any(ch, ["y/n", ">"], timeout=15,
                                             node_log=node_file)
        if matched and "y/n" in matched.lower():
            ch.send("y\r"); time.sleep(2)
            direct_read_until(ch, ">", timeout=20, node_log=node_file)

        # Attach to the system console to catch the boot sequence.
        ch.send("system console\r")
        out, matched = direct_read_until_any(
            ch, ["y/n", "ctrl-d", "type exit", "serial console", "boot loader",
                 "loader", "autoboot"], timeout=20, node_log=node_file)
        if matched and "y/n" in matched.lower():
            ch.send("y\r"); time.sleep(2)

        # Send a CR to wake the console.

        # Monitor for AUTOBOOT and LOADER.
        buf = ""
        start = time.monotonic()
        loader_seen = False
        while time.monotonic() - start < 1200:
            if _shutdown_event.is_set():
                return False
            if ch.recv_ready():
                chunk = ch.recv(4096).decode("utf-8", errors="replace")
                buf += chunk
                if node_file:
                    _par_write(node_file, chunk)
                if _session_log:
                    _session_log.log_console(f"[{label}] {chunk}")
                if "starting autoboot press ctrl-c to abort" in buf.lower():
                    for _ in range(5):
                        ch.send("\x03"); time.sleep(0.3)
                    buf = ""
                elif _LOADER_PROMPT_RE.search(buf):
                    loader_seen = True
                    break
                if len(buf) > 8192:
                    buf = buf[-4096:]
            time.sleep(0.1)

        if not loader_seen:
            print(f"   ⚠️  [{label}] LOADER not reached; aborting.")
            _slog(f"[{label}] LOADER not reached", prefix="WARN")
            return False

        # LOADER commands (NO destroy storage pods – this node is JOINING).
        # Physical zeroing, if requested, is set on every node so the
        # whole cluster ends up with consistent raid.use-physical-zeroing?
        # behaviour.
        _peer_loader_cmds = ["set-defaults", "setenv AUTO_FW_UPDATE false"]
        if _physical_zeroing:
            _peer_loader_cmds.append("setenv raid.use-physical-zeroing? true")
        _peer_loader_cmds += ["saveenv", "boot_ontap menu"]
        for cmd in _peer_loader_cmds:
            if cmd != "boot_ontap menu":
                direct_send_and_wait(ch, cmd, "LOADER", timeout=15,
                                     node_log=node_file)
            else:
                ch.send(cmd + "\r"); time.sleep(1)

        # Wait for boot menu and send option 4.
        sig_lower = ["selection (1-", "(1-9)?", "(1-11)?", "(1-12)?"]
        out_lower = ""
        s = time.monotonic()
        seen_menu = False
        while time.monotonic() - s < 1200:
            if _shutdown_event.is_set():
                return False
            if ch.recv_ready():
                chunk = ch.recv(4096).decode("utf-8", errors="replace")
                if node_file:
                    _par_write(node_file, chunk)
                if _session_log:
                    _session_log.log_console(f"[{label}] {chunk}")
                out_lower += chunk.lower()
                if any(sg in out_lower for sg in sig_lower):
                    seen_menu = True
                    break
                if len(out_lower) > 16384:
                    out_lower = out_lower[-8192:]
            time.sleep(0.1)
        if not seen_menu:
            print(f"   ⚠️  [{label}] boot menu not detected; aborting.")
            return False
        ch.send("4\r"); time.sleep(2)

        # Yes confirmations + node mgmt + join wizard.
        _auto_answer_disk_erase_prompts(ch, node_log=node_file, label=peer_bmc,
                                        is_node_add=True)

        cfg = _resolve_node_mgmt_config(peer_bmc)
        _mgmt_residual = _auto_answer_node_mgmt(ch, cfg, node_log=node_file) or ""

        # Press Enter at "press Enter to complete cluster setup".
        # The prompt may already be in the residual buffer from the last
        # management-prompt recheck window; if so send Enter immediately
        # rather than waiting for data that won't arrive.
        if "press enter to complete cluster setup" in _mgmt_residual.lower():
            print(f"\n✅ [{label}] 'Press Enter' prompt already received – sending Enter")
            _slog(f"[{label}] 'Press Enter' prompt was in residual buffer; sent Enter")
            ch.send("\r")
            time.sleep(0.5)
        else:
            _wait_and_send(ch, "press enter to complete cluster setup", "",
                           f"[{label}] Press Enter to complete cluster setup",
                           timeout=1800, node_log=node_file)

        # ── All-ready barrier ───────────────────────────────────────────────
        # Block here until every peer thread has finished LOADER/format and
        # reached this point. This guarantees that the join order is the same
        # as the peer_bmcs list, not arbitrary lock-acquisition order.
        print(f"\n⏳ [{label}] Format complete – waiting for all nodes to reach the join phase...")
        _slog(f"[{label}] reached join phase (index {join_index}); waiting at barrier")

        # Checkpoint: this peer has cleared boot-menu option 4 and the
        # disk/format work is done; it is now waiting at the join
        # barrier. On resume we know the option-4/format step does not
        # need to be repeated for this peer.
        if _checkpoint:
            try:
                _checkpoint.mark_node_done("peer_option4_done", peer_bmc)
                _slog(f"[{label}] checkpoint: peer_option4_done saved")
            except Exception:
                pass

        if join_barrier is not None:
            try:
                join_barrier.wait(timeout=1800)
                _passed_barrier = True
            except threading.BrokenBarrierError:
                print(f"   ❌ [{label}] Join barrier broken (a peer likely failed); aborting.")
                _slog(f"[{label}] join barrier broken", prefix="ERROR")
                return False
        # Record the exact moment this thread cleared the barrier; the first
        # thread to set this also fixes the shared barrier-release timestamp
        # used by the caller to split the option-4 phase from the join phase.
        _t_barrier_pass = time.monotonic()
        if barrier_release_box is not None:
            try:
                if timings_lock is not None:
                    with timings_lock:
                        if barrier_release_box[0] is None:
                            barrier_release_box[0] = _t_barrier_pass
                else:
                    if barrier_release_box[0] is None:
                        barrier_release_box[0] = _t_barrier_pass
            except Exception:
                pass
        print(f"\n✅ [{label}] All nodes ready. Joining in list order (position {join_index + 1})...")

        # ── Ordered join ─────────────────────────────────────────────────────
        # Index 0 may proceed immediately after the barrier (its event is
        # pre-set by the caller). Each subsequent node waits for the previous
        # one to finish before sending 'join'.
        if join_proceed_events is not None and join_index < len(join_proceed_events):
            if join_index > 0:
                print(f"\n⏳ [{label}] Waiting for node {join_index} to finish joining first...")
                _slog(f"[{label}] waiting for join_proceed_events[{join_index}]")
                join_proceed_events[join_index].wait(timeout=1800)

        print(f"\n🔗 [{label}] Join turn {join_index + 1}: sending 'join'...")
        _slog(f"[{label}] join turn {join_index + 1}: proceeding")
        # Stamp the moment this peer is about to send "join". The first
        # peer to reach this point also fixes the shared "first join sent"
        # timestamp used by the caller to compute join→all-healthy wall time.
        _t_join_sent = time.monotonic()
        if first_join_sent_box is not None:
            try:
                if timings_lock is not None:
                    with timings_lock:
                        if first_join_sent_box[0] is None:
                            first_join_sent_box[0] = _t_join_sent
                else:
                    if first_join_sent_box[0] is None:
                        first_join_sent_box[0] = _t_join_sent
            except Exception:
                pass
        _wait_and_send(ch, "do you want to create a new cluster or join",
                       "join", f"[{label}] -> join", timeout=900,
                       node_log=node_file)

        # ---- Post-join prompts on the peer's own channel. The cluster
        # show polling that follows runs against the *primary* channel
        # and won't ever see 2 nodes unless we keep driving the peer
        # through the rest of its join wizard.
        #
        #   1. "use this configuration? [yes]:"
        #   2. "enter the IP address of an interface on the private
        #       cluster network..."
        #   3. "username:"
        #   4. "password:"
        #
        # We use the existing cluster admin credentials (admin_password
        # arg + sp_user/peer_user) for steps 3-4, which mirrors the
        # mode-2b flow.
        print(f"\n🤖 [{label}] Driving post-join wizard on peer channel...")
        _slog(f"[{label}] driving post-join wizard")

        # 1. Confirm "use this configuration?"
        print(f"\n⏳ [{label}] Auto-confirming 'use this configuration?'...")
        direct_send_and_wait(ch, "", "[yes]:",
                             timeout=900, auto_respond="yes",
                             node_log=node_file)

        # 2. Cluster-network IP. Reuse the same lookup helper mode 2b
        # uses (silently tries cluster admin creds, then BMC creds).
        print(f"\n📡 [{label}] Looking up cluster-network IP...")
        cluster_iface_ip = _fetch_existing_cluster_ip(
            bmc_user=peer_user, bmc_password=peer_password,
        )
        print(f"\n⏳ [{label}] Waiting for cluster-network IP prompt...")
        direct_send_and_wait(
            ch, "",
            "enter the ip address of an interface on the private",
            timeout=900,
            node_log=node_file,
        )
        if not cluster_iface_ip:
            print(f"\n⚠️  [{label}] No cluster-network IP available;"
                  " peer will block on prompt.")
            if _session_log:
                _session_log.log(
                    f"[{label}] cluster-network IP unavailable; "
                    "peer will be stuck at private-IP prompt",
                    prefix="WARN",
                )
        else:
            print(f"\n✅ [{label}] Sending cluster-network IP: "
                  f"{cluster_iface_ip}")
            ch.send(cluster_iface_ip + "\r")
            if _session_log:
                _session_log.log_sent(cluster_iface_ip)
            time.sleep(0.5)

            # 3. Username — the cluster admin (defaults to "admin").
            cluster_admin_user = (
                _cluster_config.get("admin_user")
                or (
                    (_config_data.get("cluster") or {}).get("user")
                    if isinstance(_config_data, dict) else None
                )
                or "admin"
            )
            print(f"\n⏳ [{label}] Waiting for username prompt...")
            direct_send_and_wait(ch, "", "username", timeout=600,
                                 node_log=node_file)
            print(f"\n✅ [{label}] Sending username: {cluster_admin_user}")
            ch.send(cluster_admin_user + "\r")
            if _session_log:
                _session_log.log_sent(cluster_admin_user)
            time.sleep(0.5)

            # 4. Password — cluster admin password.
            print(f"\n⏳ [{label}] Waiting for password prompt...")
            direct_send_and_wait(ch, "", "password", timeout=600,
                                 node_log=node_file)
            print(f"\n✅ [{label}] Sending cluster admin password (hidden).")
            ch.send((admin_password or "") + "\r")
            if _session_log:
                _session_log.log(f"[{label}] sent cluster admin password "
                                 "(<hidden>)")
            time.sleep(0.5)

        # Wait until cluster show confirms the new node count AND every
        # row reports Health=true / Eligibility=true with no 'warning'
        # text. 10-minute total timeout, polling every 120s with operator
        # notifications on each retry. On a 2-node cluster the poller
        # will also auto-run `cluster ha modify -configured true` to
        # clear the post-join HA warning.
        print(f"\n⏳ [{label}] Waiting for cluster to show "
              f"{expected_count_after} healthy node(s)...")
        if _session_log:
            _session_log.log(f"[{label}] waiting for cluster show count "
                             f">= {expected_count_after} with all-true status")
        ok = _wait_for_cluster_nodes_healthy(
            primary_channel, expected_count_after,
            total_timeout=600, poll_interval=120, label=label,
            final_count=final_cluster_count if final_cluster_count else None,
        )
        # Stamp the moment cluster-show verification finished (regardless
        # of ok/timeout). Used both per-node (join→healthy) and aggregate
        # (first-join → last-healthy) in the session summary.
        _t_healthy_done = time.monotonic()
        if ok:
            print(f"\n✅ [{label}] Node added (cluster show confirmed all healthy).")
            _slog(f"[{label}] node added (verified, all-true)")
        else:
            print(f"\n⚠️  [{label}] Did not see {expected_count_after}"
                  " healthy node(s) in cluster show within 10 minutes; continuing.")
            if _session_log:
                _session_log.log(f"[{label}] cluster show verification "
                                 "timeout (10 min)", prefix="WARN")

        # Signal the next node in the list that it is their turn to join.
        if join_proceed_events is not None and join_index + 1 < len(join_proceed_events):
            print(f"\n✅ [{label}] Signaling node {join_index + 2} to proceed with join...")
            _slog(f"[{label}] setting join_proceed_events[{join_index + 1}]")
            join_proceed_events[join_index + 1].set()

        # Record per-node split timing for the session summary.
        if timings_record is not None and _t_barrier_pass is not None:
            _t_done = time.monotonic()
            _entry = {
                "option4": _t_barrier_pass - _t_thread_start,
                "join": _t_done - _t_barrier_pass,
                "join_to_healthy": _t_healthy_done - _t_join_sent,
                "healthy_done_mono": _t_healthy_done,
                "node_name": (_node_cfg_for(peer_bmc) or {}).get("name") or "",
                "ok": True,
            }
            try:
                if timings_lock is not None:
                    with timings_lock:
                        timings_record[peer_bmc] = _entry
                else:
                    timings_record[peer_bmc] = _entry
            except Exception:
                pass

        # Checkpoint: this peer has joined the cluster.
        if _checkpoint:
            try:
                _checkpoint.mark_node_done("peer_joined", peer_bmc)
                _slog(f"[{label}] checkpoint: peer_joined saved")
            except Exception:
                pass

        return True
    except Exception as e:
        print(f"   ❌ [{label}] Error: {e}")
        _slog(f"[{label}] thread error: {e}", prefix="ERROR")
        return False
    finally:
        # If this thread is exiting for any reason (failure or success) without
        # having reached the barrier or set its proceed-event, unblock the other
        # threads immediately so they don't hang for up to 30 minutes waiting.
        try:
            if join_barrier is not None and not _passed_barrier:
                join_barrier.abort()
        except Exception:
            pass
        try:
            if (join_proceed_events is not None
                    and join_index + 1 < len(join_proceed_events)
                    and not join_proceed_events[join_index + 1].is_set()):
                join_proceed_events[join_index + 1].set()
        except Exception:
            pass
        try:
            if ch is not None:
                ch.close()
        except Exception:
            pass
        try:
            if client is not None:
                client.close()
        except Exception:
            pass
        if node_file:
            try:
                node_file.close()
                print(f"   📝 [{label}] Log saved: {node_file.name}")
            except Exception:
                pass


def _run_2b_parallel_add(peer_bmcs, bmc_user, bmc_passwords, log):
    """Mode 2b with multiple nodes: reset all peers to LOADER in parallel,
    run option 4 and auto-answer prompts for all in parallel, then
    serialize the cluster join one node at a time (via _join_lock).

    peer_bmcs    : ordered list of BMC IPs/hostnames
    bmc_user     : default BMC username (used when no per-IP override)
    bmc_passwords: {ip: password} mapping
    log          : SessionLogger instance
    """
    if not peer_bmcs:
        return False

    # ── 1. Display nodes and confirm ────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  ➕ Mode 2b: add the following {len(peer_bmcs)} node(s) to the cluster?")
    print("  " + "─" * 58)
    _col_w = 18
    print(f"  {'#':<4} {'BMC IP':<{_col_w}} {'Node Mgmt IP':<{_col_w}} "
          f"{'Port':<8} {'Gateway':<{_col_w}}")
    print("  " + "─" * 58)
    for i, ip in enumerate(peer_bmcs, 1):
        _nc = _node_cfg_for(ip)
        _n_ip  = _nc.get("node_mgmt_ip")      or _nc.get("ip")      or "—"
        _n_prt = _nc.get("node_mgmt_port")    or _nc.get("port")    or "—"
        _n_gw  = _nc.get("node_mgmt_gateway") or _nc.get("gateway") or "—"
        print(f"  {i:<4} {ip:<{_col_w}} {_n_ip:<{_col_w}} {_n_prt:<8} {_n_gw:<{_col_w}}")
    print("=" * 60)

    # ── 2. BMC credentials ─────────────────────────────────────────────────
    # Check whether any node still needs a password collected.
    _needs_creds = [ip for ip in peer_bmcs if ip not in _peer_bmc_creds
                    or not (_peer_bmc_creds[ip] or {}).get("password")]
    if _needs_creds:
        # Ask if all passwords are the same to avoid N prompts.
        _same_pw = _prompt(
            f"\n  Are the BMC passwords the same for all {len(peer_bmcs)} node(s)? [y/n]: "
        , "n").lower()

        _shared_pw = None
        if _same_pw == "y":
            # Reuse primary (sp_host) password if it's already known;
            # otherwise prompt once.
            _primary_p = (_peer_bmc_creds.get(peer_bmcs[0]) or {}).get("password") or ""
            if _primary_p:
                _shared_pw = _primary_p
                print("  ✅ Using primary node BMC password for all nodes.")
            else:
                try:
                    _shared_pw = getpass.getpass("  BMC password for all nodes: ")
                except (EOFError, KeyboardInterrupt):
                    _shared_pw = ""

        for ip in _needs_creds:
            _nc = _node_cfg_for(ip)
            _ep_u = ((_nc.get("bmc_user") or "").strip() or bmc_user)
            _ep_p_cfg = _nc.get("bmc_password")
            if _ep_p_cfg:
                print(f"  📄 Using config credentials for {ip} (user={_ep_u})")
                _ep_p = _ep_p_cfg
            elif _shared_pw is not None:
                _ep_p = _shared_pw
            else:
                try:
                    _ep_p = getpass.getpass(
                        f"  BMC password for {_ep_u}@{ip} "
                        "(blank to reuse primary): "
                    ) or (_peer_bmc_creds.get(peer_bmcs[0]) or {}).get("password", "")
                except (EOFError, KeyboardInterrupt):
                    _ep_p = ""
            _peer_bmc_creds[ip] = {"user": _ep_u, "password": _ep_p}
            if log:
                log.log(f"2b: resolved BMC creds for {ip} (user={_ep_u})")

    # ── 3. Ping all BMC IPs ────────────────────────────────────────────────
    print(f"\n  🏓 Pinging {len(peer_bmcs)} BMC(s)...")
    _unreachable = []
    for ip in peer_bmcs:
        _ok = _silent_ping(ip)
        _sym = "✅" if _ok else "❌"
        print(f"    {_sym} {ip}")
        if not _ok:
            _unreachable.append(ip)
    if _unreachable:
        print(f"\n  ❌ {len(_unreachable)} node(s) unreachable: {', '.join(_unreachable)}")
        print("  Aborting — resolve connectivity before retrying.")
        if log:
            log.log(
                f"2b parallel add aborted: unreachable BMCs: {_unreachable}",
                prefix="ERROR",
            )
        return False

    # ── 3b. Pre-flight BMC authentication check ───────────────────────────
    print(f"\n  🔐 Testing BMC authentication ({len(peer_bmcs)} node(s))...")
    _auth_failed_nodes = []
    for _ip in list(peer_bmcs):
        _c = _peer_bmc_creds.get(_ip) or {}
        _u = _c.get("user") or bmc_user
        _p = _c.get("password") or bmc_passwords.get(_ip, "")
        while True:
            try:
                _cl, _u, _p = _ssh_connect_with_retry(
                    _ip, _u, _p, label=f"auth-check/{_ip}",
                    max_attempts=1, interactive=False,
                )
                try:
                    _cl.close()
                except Exception:
                    pass
                _peer_bmc_creds[_ip] = {"user": _u, "password": _p}
                print(f"    ✅ {_ip} (user={_u})")
                if log:
                    log.log(f"2b pre-auth OK: {_ip} (user={_u})")
                break
            except Exception as _ae:
                print(f"    ❌ {_ip} — authentication failed: {_ae}")
                if log:
                    log.log(f"2b pre-auth FAIL: {_ip}: {_ae}", prefix="WARN")
                print(f"    Re-enter credentials for {_ip} "
                      "(or leave password blank to skip this node):")
                try:
                    _u = input(f"      Username [{_u}]: ").strip() or _u
                    _p = getpass.getpass(f"      Password for {_u}@{_ip}: ")
                except (EOFError, KeyboardInterrupt):
                    _p = ""
                if not _p:
                    print(f"    ⚠️  Skipping {_ip} — no credentials entered.")
                    _auth_failed_nodes.append(_ip)
                    break
    if _auth_failed_nodes:
        print(f"\n  ❌ Authentication failed for: {', '.join(_auth_failed_nodes)}")
        _skip_ans = _prompt(
            "  Remove failed node(s) and continue, or abort? [remove/abort]: "
        , "abort").lower()
        if _skip_ans == "remove":
            peer_bmcs = [ip for ip in peer_bmcs if ip not in _auth_failed_nodes]
            if not peer_bmcs:
                print("  No nodes remaining after removing failed ones. Aborting.")
                if log:
                    log.log("2b: no nodes remain after auth failures; aborting",
                            prefix="ERROR")
                return False
            print(f"  Continuing with {len(peer_bmcs)} node(s).")
            if log:
                log.log(f"2b: removed {_auth_failed_nodes} (auth fail); "
                        f"continuing with {peer_bmcs}")
        else:
            print("  Aborting.")
            if log:
                log.log(f"2b: aborted due to auth failure on {_auth_failed_nodes}",
                        prefix="ERROR")
            return False

    # ── 4. Final confirmation ──────────────────────────────────────────────
    print()
    _confirm = _prompt(
        f"  Proceed with adding all {len(peer_bmcs)} node(s) to the cluster? [y/n]: "
    , "n").lower()
    if _confirm != "y":
        print("\n  Aborting.")
        if log:
            log.log("Mode 2b parallel add: operator declined confirmation")
        return False
    if log:
        log.log(f"Mode 2b parallel add confirmed for {len(peer_bmcs)} node(s): {peer_bmcs}")

    # Write node-add manifest so option 2c can resume if this run is interrupted.
    _write_node_add_manifest(
        nodes=[
            dict(
                bmc=addr,
                bmc_user=(_peer_bmc_creds.get(addr) or {}).get("user") or bmc_user,
                bmc_password=(_peer_bmc_creds.get(addr) or {}).get("password") or bmc_passwords.get(addr, ""),
                **{k: v for k, v in (_node_cfg_for(addr) or {}).items()
                   if k in ("node_mgmt_ip", "node_mgmt_port",
                            "node_mgmt_netmask", "node_mgmt_gateway")},
            )
            for addr in peer_bmcs
        ],
        cluster_mgmt_ip=_cluster_config.get("mgmt_ip") or "",
        cluster_admin_user=(_cluster_config.get("admin_user")
                            or ((_config_data.get("cluster") or {}).get("user")
                                if isinstance(_config_data, dict) else None)
                            or "admin"),
        cluster_admin_password=(_cluster_config.get("admin_password")
                                 or ((_config_data.get("cluster") or {}).get("password")
                                     if isinstance(_config_data, dict) else None)
                                 or ""),
    )

    # ── 2. Admin password (for join wizard + cluster show) ─────────────────
    admin_password = (
        _cluster_config.get("admin_password")
        or ((_config_data.get("cluster") or {}).get("password")
            if isinstance(_config_data, dict) else None)
        or ""
    )
    _admin_user = (
        _cluster_config.get("admin_user")
        or ((_config_data.get("cluster") or {}).get("user")
            if isinstance(_config_data, dict) else None)
        or "admin"
    )

    # ── 3. Establish primary-channel for cluster show (best-effort) ────────
    primary_channel = None
    primary_client = None
    baseline = 0
    mgmt_ip = _cluster_config.get("mgmt_ip")
    if mgmt_ip:
        print(f"\n  🔌 Connecting to cluster {mgmt_ip} for join verification...")
        _cm_user, _cm_pass = _admin_user, admin_password
        while True:
            try:
                primary_client, _cm_user, _cm_pass = _ssh_connect_with_retry(
                    mgmt_ip, _cm_user, _cm_pass,
                    label=f"cluster/{mgmt_ip}", max_attempts=1, interactive=False,
                )
                _pch = _open_shell(primary_client)
                if _login_primary_cluster_shell(_pch, _cm_pass):
                    primary_channel = _pch
                    _admin_user = _cm_user
                    admin_password = _cm_pass
                    baseline = _cluster_show_node_count(primary_channel)
                    print(f"  ✅ Connected; current cluster node count: {baseline}")
                    if log:
                        log.log(f"2b parallel: cluster mgmt OK; baseline={baseline}")
                else:
                    primary_client.close()
                    primary_client = None
                    print("  ⚠️  Could not log into cluster shell; join verification skipped.")
                    if log:
                        log.log("2b parallel: cluster shell login failed; verification skipped",
                                prefix="WARN")
                break
            except paramiko.AuthenticationException:
                print(f"  ❌ Cluster mgmt authentication failed for {_cm_user}@{mgmt_ip}.")
                if log:
                    log.log(f"2b parallel: cluster mgmt auth failed for {_cm_user}@{mgmt_ip}",
                            prefix="WARN")
                try:
                    _cm_user = input(
                        f"    Cluster mgmt username [{_cm_user}]: "
                    ).strip() or _cm_user
                    _cm_pass = getpass.getpass(
                        f"    Cluster mgmt password for {_cm_user}@{mgmt_ip}: "
                    )
                except (EOFError, KeyboardInterrupt):
                    _cm_pass = ""
                if not _cm_pass:
                    print("  Skipping cluster mgmt connection (no password entered); "
                          "join verification will be skipped.")
                    primary_channel = None
                    primary_client = None
                    break
            except Exception as _ce:
                print(f"  ⚠️  Cluster mgmt connection failed ({_ce}); verification skipped.")
                if log:
                    log.log(f"2b parallel: cluster mgmt connection failed: {_ce}", prefix="WARN")
                primary_channel = None
                primary_client = None
                break
    else:
        print("  ℹ️  Cluster mgmt IP not set; join verification will be skipped.")
        if log:
            log.log("2b parallel: no cluster mgmt IP; verification skipped")

    # ── 4. Spawn one thread per peer ────────────────────────────────────────
    if log:
        log.start_phase("2b – Parallel Node Add")

    _final_2b = (baseline + len(peer_bmcs)) if primary_channel is not None else 0
    _pending = list(peer_bmcs)  # nodes still to be successfully added
    _joined_count = 0           # number that have successfully joined so far

    while _pending:
        _n = len(_pending)
        _join_barrier = threading.Barrier(_n)
        _join_proceed = [threading.Event() for _ in range(_n)]
        _join_proceed[0].set()  # first node may proceed immediately after barrier
        _batch_results = [None] * _n

        threads = []
        for idx, addr in enumerate(_pending):
            _creds = _peer_bmc_creds.get(addr) or {}
            u = _creds.get("user") or bmc_user
            p = _creds.get("password")
            if p is None:
                p = bmc_passwords.get(addr, "")
            # Each peer needs to see (baseline + already-joined + its own slot).
            expected = (baseline + _joined_count + idx + 1
                        if primary_channel is not None else 0)
            def _run_with_result(_ri=idx, _addr=addr, _u=u, _p=p, _exp=expected):
                _batch_results[_ri] = _add_peer_node_thread(
                    _addr, _u, _p, primary_channel, admin_password, _exp,
                    _join_barrier, _join_proceed, _ri,
                    final_cluster_count=_final_2b,
                )
            t = threading.Thread(
                target=_run_with_result,
                daemon=True,
                name=f"2b-add-{addr}",
            )
            t.start()
            threads.append(t)
            print(f"  ▶️  [{addr}] Thread started.")

        print(f"\n  ⏳ Waiting for {_n} node(s) to complete "
              f"(parallel LOADER/format, serialized join)...")
        for t in threads:
            t.join()

        _joined_count += sum(1 for r in _batch_results if r)
        _failed = [_pending[i] for i, r in enumerate(_batch_results) if not r]

        if not _failed:
            break

        print(f"\n  ⚠️  {len(_failed)} node(s) did not complete: {', '.join(_failed)}")
        if log:
            log.log(f"2b: {len(_failed)} node(s) failed: {_failed}", prefix="WARN")
        _retry_ans = _prompt("  Retry failed node(s)? [y/n]: ", "n").lower()
        if _retry_ans != "y":
            break
        print(f"\n  🔁 Retrying {len(_failed)} node(s)...")
        if log:
            log.log(f"2b: operator chose to retry: {_failed}")
        _pending = _failed

    if log:
        log.end_phase()

    if primary_client:
        try:
            primary_client.close()
        except Exception:
            pass

    print("\n  ✅ Mode 2b parallel add complete.")
    if log:
        log.log("Mode 2b parallel add: all threads finished")
    return True


# ── Option 3 helpers ────────────────────────────────────────────────────────
# Small option-3-specific bookkeeping pulled out of main() so the inline
# 1/2/3 flow stays readable. The destructive primary path (system reset, boot
# menu, cluster create) is intentionally NOT extracted — it is shared with
# option 1 and would require duplicating ~1500 lines to split cleanly.

def _option3_init_checkpoint(ctx, sp_host, peer_bmcs, config_path):
    """Detect any prior option-3 checkpoint and initialise a fresh one.

    If a prior checkpoint shows the primary node already had its cluster
    created (``primary_setup_done`` set, ``option3_complete`` not set), warn
    the operator that re-running option 3 will destroy the cluster and
    suggest option 2c instead. On confirmation (or no destructive risk) the
    prior checkpoint is cleared and a fresh one initialised.

    Mutates ``ctx.checkpoint`` and syncs back to the legacy ``_checkpoint``
    global so downstream callers that still read the global see the new
    value. Calls ``sys.exit(0)`` if the operator aborts.
    """
    # Refresh ctx from globals first so the trailing apply_to_globals() call
    # only overwrites the field we actually mutated (ctx.checkpoint).
    ctx.refresh_from_globals()
    session_log = ctx.session_log

    prior = CheckpointManager()
    if prior.load():
        prior_primary = prior.is_done("primary_setup_done")
        prior_joined = prior.nodes_done_for("peer_joined")
        prior_complete = prior.is_done("option3_complete")
        prior_bootmenu = prior.is_done("primary_bootmenu_done")
        prior_cluster = prior.is_done("cluster_formed")
        prior_opt4 = prior.nodes_done_for("peer_option4_done")
        _print_banner("🔖 Prior option-3 checkpoint found")
        print(f"     Mode          : {prior.mode}")
        print(f"     BMC IPs       : {', '.join(prior.bmc_ips)}")
        if prior_bootmenu:
            print("     primary_bootmenu_done : ✅ (primary cleared boot menu)")
        if prior_cluster:
            print("     cluster_formed        : ✅ (cluster create succeeded)")
        if prior_primary:
            print("     primary_setup_done    : ✅ (cluster already created)")
        if prior_opt4:
            print(f"     peer_option4_done     : {', '.join(prior_opt4)}")
        if prior_joined:
            print(f"     peer_joined           : {', '.join(prior_joined)}")
        if prior_complete:
            print("     option3_complete      : ✅")
        print("=" * 60)

        if prior_primary and not prior_complete:
            print("\n  ⚠️  The prior run created a cluster on the primary node.")
            print("      Re-running option 3 will SYSTEM RESET that primary")
            print("      and DESTROY the existing cluster.")
            print("      To finish adding the remaining peer(s) without")
            print("      re-resetting the primary, exit now and run option 2c")
            print("      (Resume node additions) instead.")
            ans = _prompt(
                "\n  Proceed with full option 3 (destroys cluster) [y/N]: "
            , "n").lower()
            if ans != "y":
                print("  ↩️  Aborting option 3. Run option 2c to resume node additions.")
                if session_log:
                    session_log.log(
                        "option 3 aborted by operator at resume prompt; cluster already up"
                    )
                    session_log.set_outcome(
                        "ABORT", "operator declined to destroy existing cluster"
                    )
                    session_log.close()
                sys.exit(0)
            prior.clear()
            if session_log:
                session_log.log("operator confirmed; cleared prior option-3 checkpoint")
        else:
            prior.clear()
            if session_log:
                session_log.log("cleared prior option-3 checkpoint (no destructive risk)")

    try:
        all_bmcs = [sp_host] + list(peer_bmcs or [])
        cp = CheckpointManager()
        cp.init_run(
            mode="3",
            bmc_ips=all_bmcs,
            log_dir=(session_log.log_dir
                     if session_log and hasattr(session_log, "log_dir") else ""),
            config_path=(config_path or ""),
        )
        ctx.checkpoint = cp
        ctx.apply_to_globals()
        if session_log:
            session_log.log(
                f"option 3 checkpoint initialised for BMCs: {all_bmcs}"
            )
    except Exception as exc:
        if session_log:
            session_log.log(f"option 3 checkpoint init failed: {exc}", prefix="WARN")


def _option3_finalize(ctx, cluster_mgmt_ip):
    """Mark option 3 complete, clear the checkpoint, print the completion
    block, close the session log, and exit cleanly.
    """
    checkpoint = ctx.checkpoint
    session_log = ctx.session_log
    if checkpoint:
        try:
            checkpoint.mark_done("option3_complete")
            checkpoint.clear()
        except Exception:
            pass
    mgmt_ip = cluster_mgmt_ip or "<cluster-management-ip>"
    _print_banner("✅ End-to-end configuration complete.")
    print(f"  To login to the cluster, SSH to {mgmt_ip} or use a web")
    print(f"  browser to access https://{mgmt_ip}")
    print("=" * 60)
    if session_log:
        session_log.log("Mode 3 end-to-end completed successfully")
        session_log.log(f"SSH to {mgmt_ip} or https://{mgmt_ip}")
        session_log.set_outcome("PASS", "end-to-end auto initialize complete")
        try:
            session_log.close()
        except Exception:
            pass
    sys.exit(0)


def add_peer_nodes_parallel(primary_channel, peer_bmcs, admin_password):
    """Spawn one thread per peer BMC, all running the auto-add flow in
    parallel. The "create/join" answer + cluster-show verification is
    serialized via `_join_lock`, so only one peer joins at a time even
    though everything else (LOADER, format, node-mgmt setup) runs together.
    """
    if not peer_bmcs:
        return

    # Checkpoint: primary is up and we're about to add peers. Record this
    # milestone now so a re-run can detect that the cluster already exists
    # and steer the operator to option 2c rather than re-running option 3
    # (which would system-reset the primary and destroy the cluster).
    if _checkpoint:
        try:
            _checkpoint.mark_done("primary_setup_done")
        except Exception:
            pass

    # Skip peers that are already marked peer_joined from a prior run.
    if _checkpoint:
        _already = set(_checkpoint.nodes_done_for("peer_joined"))
        if _already:
            _skip = [p for p in peer_bmcs if p in _already]
            peer_bmcs = [p for p in peer_bmcs if p not in _already]
            if _skip:
                print("\n  🔖 Checkpoint: skipping peer(s) already joined: "
                      f"{', '.join(_skip)}")
                if _session_log:
                    _session_log.log(
                        f"checkpoint resume: skipping already-joined peers: {_skip}"
                    )
            if not peer_bmcs:
                print("  ✅ All peers already joined per checkpoint; nothing to do.")
                if _session_log:
                    _session_log.log("checkpoint resume: all peers already joined")
                return

    _print_banner(f"🚀 Mode 3: parallel auto-add for {len(peer_bmcs)} peer node(s)")
    print(f"  Peers: {', '.join(peer_bmcs)}")
    # Track split timings: option-4 (parallel format/LOADER prep up to the
    # join barrier) vs. the sequential node-join wall time. The threads
    # populate _m3_peer_timings keyed by BMC IP; the barrier-release timestamp
    # is captured by the first thread to clear the barrier. _m3_first_join_box
    # is stamped by the first thread to send "join" so we can report the
    # wall-clock time from first cluster-join to all-nodes-healthy.
    _m3_peer_timings: "dict[str, dict]" = {}
    _m3_barrier_release_box = [None]
    _m3_first_join_box = [None]
    _m3_timings_lock = threading.Lock()
    _t_mode3_start = time.monotonic()
    if _session_log:
        _session_log.log(f"Spawning auto-add threads for: {peer_bmcs}")

    # Login to primary cluster shell so we can run `cluster show` for join
    # verification.
    if not _login_primary_cluster_shell(primary_channel, admin_password):
        print("⚠️  Could not log in to primary cluster shell; cluster show "
              "verification will be skipped.")
        if _session_log:
            _session_log.log("Primary cluster shell login failed; verification skipped",
                             prefix="WARN")
    else:
        baseline = _cluster_show_node_count(primary_channel)
        _slog(f"Baseline cluster node count: {baseline}")

    # Write node-add manifest so option 2c can resume if this run is interrupted.
    _write_node_add_manifest(
        nodes=[
            dict(
                bmc=addr,
                bmc_user=(_peer_bmc_creds.get(addr) or {}).get("user") or "admin",
                bmc_password=(_peer_bmc_creds.get(addr) or {}).get("password") or "",
                **{k: v for k, v in (_node_cfg_for(addr) or {}).items()
                   if k in ("node_mgmt_ip", "node_mgmt_port",
                            "node_mgmt_netmask", "node_mgmt_gateway")},
            )
            for addr in peer_bmcs
        ],
        cluster_mgmt_ip=_cluster_config.get("mgmt_ip") or "",
        cluster_admin_user=(_cluster_config.get("admin_user") or "admin"),
        cluster_admin_password=(admin_password or ""),
    )

    _m3_total = len(peer_bmcs)
    _m3_pending = list(peer_bmcs)  # nodes still to be successfully added
    _m3_joined = 0                 # peers that have joined so far

    while _m3_pending:
        _n3 = len(_m3_pending)
        _join_barrier3 = threading.Barrier(_n3)
        _join_proceed3 = [threading.Event() for _ in range(_n3)]
        _join_proceed3[0].set()  # first node proceeds immediately after barrier
        _m3_results = [None] * _n3

        threads = []
        for idx, addr in enumerate(_m3_pending):
            creds = _peer_bmc_creds.get(addr, {"user": None, "password": None})
            u = creds.get("user") or "admin"
            p = creds.get("password") or ""
            # 1 primary + already-joined peers + this peer's slot.
            expected = _m3_joined + idx + 2
            def _run_m3(_ri=idx, _addr=addr, _u=u, _p=p, _exp=expected):
                _m3_results[_ri] = _add_peer_node_thread(
                    _addr, _u, _p, primary_channel, admin_password, _exp,
                    _join_barrier3, _join_proceed3, _ri,
                    final_cluster_count=_m3_total + 1,
                    timings_record=_m3_peer_timings,
                    barrier_release_box=_m3_barrier_release_box,
                    timings_lock=_m3_timings_lock,
                    first_join_sent_box=_m3_first_join_box,
                )
            t = threading.Thread(
                target=_run_m3,
                daemon=True,
                name=f"peer-add-{addr}",
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        _m3_joined += sum(1 for r in _m3_results if r)
        _m3_failed = [_m3_pending[i] for i, r in enumerate(_m3_results) if not r]

        if not _m3_failed:
            break

        print(f"\n⚠️  {len(_m3_failed)} node(s) did not complete: {', '.join(_m3_failed)}")
        if _session_log:
            _session_log.log(f"peer add: {len(_m3_failed)} node(s) failed: {_m3_failed}",
                             prefix="WARN")
        _m3_retry_ans = _prompt("  Retry failed node(s)? [y/n]: ", "n").lower()
        if _m3_retry_ans != "y":
            break
        print(f"\n🔁 Retrying {len(_m3_failed)} node(s)...")
        if _session_log:
            _session_log.log(f"peer add: operator chose to retry: {_m3_failed}")
        _m3_pending = _m3_failed

    if _session_log:
        _t_mode3_end = time.monotonic()
        _barrier_release = _m3_barrier_release_box[0]
        if _barrier_release is not None:
            _option4_wall = max(0.0, _barrier_release - _t_mode3_start)
            _joins_wall = max(0.0, _t_mode3_end - _barrier_release)
        else:
            # No node ever cleared the barrier (all failed during option-4).
            _option4_wall = max(0.0, _t_mode3_end - _t_mode3_start)
            _joins_wall = 0.0

        _any_failed = any(r is False for r in (_m3_results or []))
        _opt4_outcome = "PASSED (WITH ERRORS)" if _any_failed and _barrier_release is None else "PASS"
        _join_outcome = "PASSED (WITH ERRORS)" if _any_failed and _barrier_release is not None else "PASS"

        _session_log.record_phase(
            "Parallel Peer Option 4 (mode 3)", _option4_wall, outcome=_opt4_outcome,
        )
        _session_log.record_phase(
            "Node join total", _joins_wall, outcome=_join_outcome,
        )
        # Per-node join breakdown (only nodes that reached the join phase
        # populated _m3_peer_timings).
        for _bmc in peer_bmcs:
            _t = _m3_peer_timings.get(_bmc)
            if not _t:
                continue
            _nm = _t.get("node_name") or ""
            _node_label = (
                f"Node [{_nm} - {_bmc}]" if _nm else f"Node [{_bmc}]"
            )
            _session_log.add_phase_subtiming(
                "Node join total", _node_label, _t.get("join", 0.0),
            )

        # Aggregate: wall-clock time from the first peer's "join" send to
        # the moment the last peer's cluster-show health check completed.
        _first_join = _m3_first_join_box[0]
        _last_healthy = max(
            (_t.get("healthy_done_mono") or 0.0)
            for _t in _m3_peer_timings.values()
        ) if _m3_peer_timings else 0.0
        if _first_join is not None and _last_healthy > _first_join:
            _session_log.add_phase_subtiming(
                "Node join total",
                "Join → all nodes healthy",
                _last_healthy - _first_join,
            )
    print("\n✅ Parallel peer auto-add complete.")


def auto_complete_initialization(channel, bmc_host=None):
    """Drive option-9 → option-4 cluster init non-interactively for mode 1b.

    Sequence:
      1. Auto-answer 'no' to the storage-availability-zone destroy warning.
      2. Wait for the 2nd boot menu and select option 4.
      3. Auto-answer 'yes' to zero-disks / erase / type-yes prompts.
      4. Supply node management port/IP/netmask/gateway from retained config
         (fall back to interactive entry per-field when missing).

    Remaining setup-wizard prompts (cluster name, admin password, etc.) are
    left to the interactive session that runs after this function returns.
    """
    print("\n🤖 Mode 1b: automated cluster initialization in progress...")
    if _session_log:
        _session_log.start_phase("Auto Cluster Init (1b)")
        _session_log.log("Mode 1b automated init starting after option 9 sent")

    # 1) Storage availability zone warning -> "no"
    print("\n⏳ Waiting for storage-availability-zone warning (auto-answer 'no')...")
    _slog("Waiting for storage-availability-zone warning")
    direct_send_and_wait(
        channel, "", "storage availability zone will be destroyed",
        timeout=1800, auto_respond="no",
    )

    # 2) Wait for the *second* boot menu and select option 4.
    print("\n⏳ Waiting for second boot menu (auto-select option 4)...")
    _slog("Waiting for second boot menu (option 4)")
    sig_lower = ["selection (1-", "(1-9)?", "(1-11)?", "(1-12)?"]
    output_lower = ""
    start = time.monotonic()
    found = False
    while time.monotonic() - start < 2400:
        if _shutdown_event.is_set():
            return
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            sys.stdout.write(chunk)
            sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            output_lower += chunk.lower()
            if any(s in output_lower for s in sig_lower):
                found = True
                break
            if len(output_lower) > 16384:
                output_lower = output_lower[-8192:]
        time.sleep(0.1)

    if not found:
        print("⚠️  2nd boot menu not detected within 2400s; aborting auto-init.")
        if _session_log:
            _session_log.log("2nd boot menu not detected within 2400s", prefix="WARN")
            _session_log.end_phase()
        return

    drain_channel(channel, seconds=1)
    print("🔢 Selecting option 4 (Initialize and configure)...")
    if _session_log:
        _session_log.log("2nd boot menu detected; auto-selecting option 4")
        _session_log.log_sent("4")
    channel.send("4\r")
    time.sleep(2)

    # 3) Yes confirmations.
    _auto_answer_disk_erase_prompts(channel, label=bmc_host or "",
                                    is_node_add=False)

    # 4) Node management config.
    cfg = _resolve_node_mgmt_config(bmc_host)
    print("\n📋 Node management config to apply:")
    for k in ("port", "ip", "netmask", "gateway"):
        v = cfg.get(k)
        print(f"   {k:<8} = {v if v else '(prompt manually)'}")
    _slog(f"Node mgmt config to use: {cfg}")
    _auto_answer_node_mgmt(channel, cfg)

    print("\n✅ Mode 1b auto-init complete; driving cluster setup wizard...")
    if _session_log:
        _session_log.log("Auto-init phase complete; transitioning to wizard automation")
        _session_log.end_phase()

    # Drive the post-node-mgmt cluster setup wizard from gathered values.
    wizard_ok = _run_cluster_setup_wizard(channel)
    if wizard_ok is False:
        print("\n❌ Cluster setup wizard failed – cannot proceed. Exiting.")
        if _session_log:
            _session_log.log(
                "Cluster setup wizard returned failure; aborting", prefix="ERROR"
            )
            _session_log.set_outcome("FAIL", "cluster setup wizard failed")
            try:
                _session_log.close()
            except Exception:
                pass
        sys.exit(1)


def handle_loader_commands(channel, client, sp_host, sp_user, sp_pass):
    print("\n⏳ Setting LOADER boot options...")
    if _session_log:
        _session_log.end_phase()  # End AUTOBOOT/LOADER Monitoring
        _session_log.start_phase("LOADER Commands")
        _session_log.log("LOADER prompt detected – running boot configuration commands")

    drain_channel(channel, seconds=1)

    # Send a bare CR to provoke a fresh LOADER prompt echo; the calling loop
    # already confirmed LOADER is active, so this should respond instantly.
    channel.send("\r")
    output = direct_read_until(channel, "LOADER", timeout=5)
    if "loader" not in output.lower():
        print("⚠️  No LOADER prompt seen, attempting commands anyway...")

    loader_commands = get_loader_commands()

    _slog(f"LOADER commands for mode {_operation_mode}: {loader_commands}")

    for command in loader_commands:
        # When netboot-before-reinit is active, skip boot_ontap menu – the
        # node will boot into the menu naturally after the netboot install.
        if command == "boot_ontap menu" and _netboot_before_reinit:
            _slog("Skipping 'boot_ontap menu' – netboot will handle boot")
            print("\n  ℹ️  Skipping 'boot_ontap menu' (netboot-install requested).")
            continue
        _slog(f"Running LOADER command: {command}")
        if command != "boot_ontap menu":
            output = direct_send_and_wait(channel, command, "LOADER", timeout=15)
            if "loader" not in output.lower():
                print(f"⚠️  No LOADER prompt after '{command}', continuing anyway...")
        else:
            channel.send(command + "\r")
            if _session_log:
                _session_log.log_sent(command)
            time.sleep(1)

        # After set-defaults, verify the node's boot DNA. Only DNA 3088 is
        # supported by this script; any other value indicates an unsupported
        # platform and we abort with a clear message.
        if command == "set-defaults":
            if not _verify_boot_dna(channel):
                if _session_log:
                    _session_log.end_phase(outcome="FAIL", note="unsupported boot DNA")
                    _session_log.set_outcome("FAIL", "unsupported boot DNA")
                    _session_log.close()
                sys.exit(1)

    # ── Netboot-before-reinit hook ─────────────────────────────────────────
    # When the operator answered 'y' to the 1a/1b ONTAP-version prompt, skip
    # boot_ontap menu and instead do ifconfig + netboot on the primary node.
    # After the install completes and the node reboots, the normal boot-menu
    # selection below will pick up the new ONTAP boot menu (option 9 → init).
    if _netboot_before_reinit:
        if _session_log:
            _session_log.end_phase()  # End LOADER Commands
            _session_log.start_phase("Netboot ONTAP Install")
        print("\n  🌐 Netboot-before-reinit: selecting ONTAP package...")
        src_type, src_value = _find_upgrade_package()
        if src_type is None:
            print("  ❌ No package selected. Aborting.")
            if _session_log:
                _session_log.end_phase(outcome="FAIL", note="no package selected")
                _session_log.set_outcome("FAIL", "no package selected for netboot")
                _session_log.close()
            sys.exit(1)
        _nb_httpd = None
        if src_type == "file":
            _nb_t, _nb_pkg_url, _nb_httpd = _start_http_server(src_value)
            print(f"  🌐 HTTP server started: {_nb_pkg_url}")
        else:
            _nb_pkg_url = src_value
        ok = _run_netboot_install_sequence(
            channel, _nb_pkg_url, node_label="primary", log=_session_log
        )
        if _nb_httpd:
            try:
                _nb_httpd.shutdown()
            except Exception:
                pass
        if not ok:
            print("  ❌ Netboot install failed.")
            if _session_log:
                _session_log.end_phase(outcome="FAIL", note="netboot install failed")
                _session_log.set_outcome("FAIL", "netboot install failed")
                _session_log.close()
            sys.exit(1)
        if _session_log:
            _session_log.end_phase()
        # Node is now rebooting with new ONTAP; fall through to boot menu wait.

    if _session_log and not _netboot_before_reinit:
        _session_log.end_phase()  # End LOADER Commands
    if _session_log:
        _session_log.start_phase("Boot Menu Selection")

    if not wait_for_boot_menu_and_select(channel, timeout=1800):
        print("\n⚠️  Falling back to manual menu selection...")
        _slog("Auto-select failed, falling back to manual input", prefix="WARN")
        drain_channel(channel, seconds=5)
        while True:
            try:
                user_option = input("\nEnter a numeric option from the menu: ")
                if not user_option.isdigit():
                    print("⚠️  Invalid input. Please enter a numeric value.")
                    continue
                if _session_log:
                    _session_log.log_user_input(f"Manual boot menu selection: {user_option}")
                    _session_log.log_sent(user_option)
                channel.send(user_option + "\r")
                break
            except (EOFError, KeyboardInterrupt):
                _shutdown_event.set()
                return

    if _session_log:
        _session_log.end_phase()  # End Boot Menu Selection

    # Checkpoint: primary cleared the ONTAP boot menu (option 9 for mode
    # 1b/3, option 4 for mode 2). Wizard is about to begin. Mode 3 init
    # always creates a checkpoint at L14952; other modes only have one
    # if --resume was supplied.
    if _checkpoint:
        try:
            _checkpoint.mark_done("primary_bootmenu_done")
            _slog("checkpoint: primary_bootmenu_done saved")
        except Exception:
            pass

    if _auto_setup:
        # Mode 1b / 3: auto_complete_initialization drives the full cluster
        # setup wizard (and, for mode 3, parallel peer auto-add) internally.
        # Nothing remains for an interactive session, so return without
        # entering InteractiveSession (which would print the misleading
        # "Session is now fully interactive" banner mid-boot).
        auto_complete_initialization(channel, bmc_host=sp_host)
        return
    elif _auto_add:
        auto_complete_join(channel, client, sp_host, sp_user, sp_pass,
                           bmc_host=sp_host)
        # Operator may have requested another node, or asked to exit. In
        # either case, skip the InteractiveSession and let main() decide.
        if _add_another_node_request is not None or _shutdown_event.is_set():
            return

    if _session_log:
        _session_log.start_phase("Interactive Session")
        _session_log.log("Switching to interactive mode (Phase 3 – passive)")

    session = InteractiveSession(channel, client, sp_host, sp_user, sp_pass)
    session.run()


# ---------------------------------------------------------------------------
# AUTOBOOT/LOADER monitoring
# ---------------------------------------------------------------------------

def monitor_for_autoboot_and_loader(channel, client, sp_host, sp_user, sp_pass):
    option, description = get_boot_menu_option()

    print("\nMonitoring for AUTOBOOT or LOADER prompt... 👀")
    print("  ➡️  AUTOBOOT will be interrupted automatically with Ctrl+C")
    print("  ➡️  LOADER prompt will trigger boot configuration commands")
    print(f"  ➡️  Boot menu option {option} ({description}) will be selected automatically")
    print("  ➡️  After that, session becomes fully interactive\n")
    _slog("Phase 1: Monitoring for AUTOBOOT/LOADER (active interruption mode)")

    output_buffer = ""

    try:
        while not _shutdown_event.is_set():
            if not is_session_alive(client, channel):
                print("\n⚠️  Session dropped during monitoring. Reconnecting...")
                _slog("Session dropped during monitoring", prefix="WARN")
                client, channel = reconnect_to_sp(sp_host, sp_user, sp_pass)
                if client is None:
                    print("❌ Reconnection failed. Press Ctrl+C to exit...")
                    try:
                        while True:
                            time.sleep(1)
                    except KeyboardInterrupt:
                        break
                    break

                output, matched = direct_read_until_any(channel, ["y/n", ">"], timeout=15)
                if matched and "y/n" in matched.lower():
                    print("⚠️  Existing session detected during reconnect, taking over...")
                    if _session_log:
                        _session_log.log("Auto-taking over existing session during reconnect")
                        _session_log.log_sent("y")
                    channel.send("y\r")
                    time.sleep(2)
                    direct_read_until(channel, ">", timeout=15)

                enter_system_console(channel)
                output_buffer = ""
                continue

            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                output_buffer += chunk
                sys.stdout.write(chunk)
                sys.stdout.flush()
                if _session_log:
                    _session_log.log_console(chunk)

                if "starting autoboot press ctrl-c to abort" in output_buffer.lower():
                    print("\n🛑 AUTOBOOT detected! Sending Ctrl+C to interrupt...")
                    _slog("AUTOBOOT detected – sending Ctrl+C to interrupt")
                    for _ in range(5):
                        channel.send("\x03")
                        time.sleep(0.3)
                    print("✅ Ctrl+C sent.")
                    _slog("Ctrl+C sent to interrupt AUTOBOOT")
                    output_buffer = ""

                # OPT: use pre-compiled module-level regex instead of re.search
                # with a raw string literal recompiled on every iteration.
                elif _LOADER_PROMPT_RE.search(output_buffer[-200:]):
                    _slog("LOADER prompt detected")
                    handle_loader_commands(channel, client, sp_host, sp_user, sp_pass)
                    break

                if len(output_buffer) > 8192:
                    output_buffer = output_buffer[-4096:]

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n👋 User interrupted. Exiting...")
        _slog("User interrupted during monitoring (Ctrl+C)")
    except (OSError, EOFError, paramiko.SSHException) as e:
        print(f"\n⚠️  Connection error during monitoring: {e}")
        _slog(f"Connection error during monitoring: {e}", prefix="ERROR")
        print("Press Ctrl+C to exit...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------
# Module-level helpers shared across main() mode blocks
# ---------------------------------------------------------------------------

def _cfg_str(v):
    """Return *v* when it is a non-empty, non-whitespace string; else None.

    Distinguishes "value present in config" from "key absent / blank / wrong
    type".  Identical to the one-liner lambdas (_s44, _s45, _s46) that each
    mode block previously defined for itself.
    """
    if isinstance(v, str) and v.strip():
        return v
    return None


def _make_session_log(label: str) -> "SessionLogger":
    """Create a SessionLogger, assign it to the module-level *_session_log*
    global, register a safety-net ``atexit`` handler so the log is always
    flushed and closed on normal process exit, log *label*, and return the
    new logger.

    Replaces the 5× near-identical boilerplate blocks that previously
    appeared inline in each mode section of ``main()``.
    """
    global _session_log
    _session_log = SessionLogger(bg_mode=_bg_mode)

    def _atexit_close():
        if _session_log and not _session_log._file.closed:
            try:
                _session_log.close()
            except Exception:
                pass

    atexit.register(_atexit_close)
    if label:
        _session_log._operation_label = label
        _session_log.log(label)
    return _session_log


# ---------------------------------------------------------------------------
# Node-add manifest  (option 2c: Resume node additions)
# ---------------------------------------------------------------------------

def _write_node_add_manifest(nodes, cluster_mgmt_ip="",
                             cluster_admin_user="admin",
                             cluster_admin_password=""):
    """Write (or overwrite) the node-add manifest JSON.

    Called just before parallel add threads start so that a run interrupted
    mid-flight can be resumed with option 2c.

    *nodes* is a list of dicts, each containing:
      bmc, bmc_user, bmc_password,
      node_mgmt_ip, node_mgmt_port, node_mgmt_netmask, node_mgmt_gateway

    The manifest is written to two locations:
      1. {script_dir}/configs/node_add_manifest.json  (canonical location)
      2. {script_dir}/configs/last_node_add_manifest.json  (pointer / latest)

    Note: bmc_password is stored in the manifest so that 2c can re-authenticate
    without prompting. The manifest lives alongside other log files that should
    be stored on a secured admin host. Remove the file when no longer needed.
    """
    global _last_node_add_manifest

    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = os.getcwd()
    configs_dir = os.path.join(script_dir, "configs")
    os.makedirs(configs_dir, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "cluster_mgmt_ip": cluster_mgmt_ip,
        "cluster_admin_user": cluster_admin_user,
        "cluster_admin_password": cluster_admin_password,
        "nodes": nodes,
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_path = os.path.join(configs_dir, f"node_add_manifest_{ts}.json")
    pointer_path = os.path.join(configs_dir, "last_node_add_manifest.json")

    for path in (session_path, pointer_path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
        except Exception as _mw_e:
            _slog(f"Could not write node-add manifest to {path}: {_mw_e}",
                  prefix="WARN")

    _last_node_add_manifest = session_path
    _slog(f"Node-add manifest written: {session_path} ({len(nodes)} node(s))")
    return session_path


def _get_cluster_node_mgmt_ips(channel, cluster_name=None):
    """Return {node_name: mgmt_ip} for every node currently in the cluster.

    Runs `cluster show` to enumerate node names, then a single
    `net int show -vserver {cluster_name} -role node-mgmt -fields address`
    to fetch all node-management LIF addresses at once.
    The cluster name is extracted from the ONTAP prompt in the cluster show
    output if not explicitly supplied.
    Returns an empty dict if the cluster shell is unreachable or no nodes
    are found.
    """
    result = {}
    # ── 1. Get node names from cluster show ──────────────────────────────
    with _primary_shell_lock:
        cs_out = _run_cluster_command(channel, "cluster show", timeout=30)
    dashes_seen = False
    table_done = False
    node_names = []
    for raw_line in cs_out.splitlines():
        s = raw_line.strip()
        if not s:
            if dashes_seen:
                table_done = True
            continue
        if table_done:
            continue
        if "::" in s or s.lower().startswith("cluster show"):
            continue
        if "entries were displayed" in s.lower():
            break
        if s.lower().startswith("warning"):
            break
        if set(s) <= {"-", " "}:
            dashes_seen = True
            continue
        if dashes_seen:
            tokens = s.split()
            if tokens:
                node_names.append(tokens[0])

    # ── 2. Derive cluster name from prompt if not supplied ────────────────
    # The ONTAP prompt embedded in cs_out looks like  "clustername::> "
    if not cluster_name:
        _prompt_m = re.search(r'(\S+)::\*?>', cs_out)
        if _prompt_m:
            cluster_name = _prompt_m.group(1)

    # ── 3. Single net int show using the cluster name as vserver ──────────
    if cluster_name:
        try:
            cmd = (f"net int show -vserver {cluster_name} -role node-mgmt "
                   f"-fields address")
            with _primary_shell_lock:
                ni_out = _run_cluster_command(channel, cmd, timeout=30)
            ni_dashes = False
            for line in ni_out.splitlines():
                s2 = line.strip()
                if not s2:
                    continue
                if "::" in s2 or s2.lower().startswith("net int"):
                    continue
                if "entries were displayed" in s2.lower():
                    break
                if set(s2) <= {"-", " "}:
                    ni_dashes = True
                    continue
                if ni_dashes:
                    # Output columns: vserver  lif  address
                    # vserver = cluster name (same on every row).
                    # lif name starts with the node name, e.g. node1_mgmt1.
                    parts = s2.split()
                    if len(parts) >= 3 and _is_valid_ipv4(parts[2]):
                        _lif, _addr = parts[1], parts[2]
                        # Match LIF name to node name by prefix.
                        for _nn in node_names:
                            if _lif.startswith(_nn):
                                result[_nn] = _addr
                                break
                    elif len(parts) == 2 and _is_valid_ipv4(parts[1]):
                        _lif, _addr = parts[0], parts[1]
                        for _nn in node_names:
                            if _lif.startswith(_nn):
                                result[_nn] = _addr
                                break
        except Exception as _ni_e:
            _slog(f"net int show (vserver {cluster_name}) failed: {_ni_e}",
                  prefix="WARN")
    else:
        _slog("Could not determine cluster name; node-mgmt IP query skipped.",
              prefix="WARN")

    return result


def _run_2c_resume():
    """Drive option 2c: locate a node-add manifest and retry any nodes that
    have not yet joined the cluster.

    Workflow:
      1. Locate and load a manifest written by a previous 2b/3 run.
      2. Connect to the cluster management IP.
      3. Use `cluster show` + `net int show` to determine which manifest
         nodes are already present in the cluster (matched by node-mgmt IP).
      4. Re-run the parallel add for only the missing nodes.

    Returns True on success, False on abort or hard failure.
    """
    _make_session_log("Mode 2c: resume node additions")
    _print_banner("↩️   2c: Resume node additions")

    # ── 1. Locate manifest ────────────────────────────────────────────────
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = os.getcwd()
    configs_dir = os.path.join(script_dir, "configs")
    pointer_path = os.path.join(configs_dir, "last_node_add_manifest.json")

    manifest_candidates = []
    if os.path.isfile(pointer_path):
        manifest_candidates.append(pointer_path)
    # Also pick up timestamped manifests in configs/ (node_add_manifest_*.json).
    import glob as _glob
    for _cp in sorted(_glob.glob(os.path.join(configs_dir, "node_add_manifest_*.json")), reverse=True):
        if _cp not in manifest_candidates:
            manifest_candidates.append(_cp)
    # Also discover reinit config JSON files in configs/ and script dir
    # (they contain secondary_nodes / primary_node and cluster.clus_mgmt_address).
    _reconfig_scan_dirs = [d for d in (configs_dir, script_dir) if os.path.isdir(d)]
    for _scan_dir in _reconfig_scan_dirs:
        for _cp in sorted(_glob.glob(os.path.join(_scan_dir, "*.json")), reverse=True):
            if os.path.basename(_cp) in ("last_node_add_manifest.json",) or \
               os.path.basename(_cp).startswith("node_add_manifest_"):
                continue   # already handled above
            try:
                with open(_cp, "r", encoding="utf-8") as _cf:
                    _cj = json.load(_cf)
                if "secondary_nodes" in _cj or "primary_node" in _cj:
                    if _cp not in manifest_candidates:
                        manifest_candidates.append(_cp)
            except Exception:
                pass

    manifest_path = None
    if manifest_candidates:
        if len(manifest_candidates) == 1:
            print(f"\n  Found manifest: {manifest_candidates[0]}")
            _ma = _prompt("  Use this manifest? [Y/n]: ", "y").lower()
            if _ma in ("", "y", "yes"):
                manifest_path = manifest_candidates[0]
        else:
            _show_n = min(10, len(manifest_candidates))
            print(f"\n  Found {len(manifest_candidates)} manifest file(s):")
            for _mi, _mp in enumerate(manifest_candidates[:_show_n], 1):
                print(f"    {_mi}. {_mp}")
            print("    0. Enter path manually")
            while True:
                _ms = _prompt(
                    f"  Select [0-{_show_n}]: "
                , "0")
                if _ms == "0":
                    break
                if _ms.isdigit() and 1 <= int(_ms) <= _show_n:
                    manifest_path = manifest_candidates[int(_ms) - 1]
                    break
                print("  ⚠️  Invalid selection.")

    # ── Config-file fallback ──────────────────────────────────────────────
    # When no manifest file was found or selected, synthesize node list from
    # the loaded reinit config (secondary_nodes / legacy nodes[1:]).
    # The user is only prompted for a manual path if neither source works.
    manifest_nodes: list         = []
    cluster_mgmt_ip: str         = ""
    cluster_admin_user: str      = "admin"
    cluster_admin_password: str  = ""
    _manifest_source: str        = ""   # for display only

    if manifest_path:
        # ── 2a. Load chosen file (manifest or reconfig JSON) ───────────────
        try:
            with open(manifest_path, "r", encoding="utf-8") as _mf:
                _mdata = json.load(_mf)
        except Exception as _me:
            print(f"  ❌ Could not read file: {_me}")
            return False

        if "nodes" in _mdata and isinstance(_mdata["nodes"], list):
            # ── Standard node-add manifest ──────────────────────────────
            manifest_nodes         = _mdata.get("nodes") or []
            cluster_mgmt_ip        = _mdata.get("cluster_mgmt_ip") or ""
            cluster_admin_user     = _mdata.get("cluster_admin_user") or "admin"
            cluster_admin_password = _mdata.get("cluster_admin_password") or ""
            _manifest_source       = manifest_path
            print(f"\n  Manifest : {manifest_path}")
            print(f"  Created  : {_mdata.get('created_at', 'unknown')}")
        else:
            # ── Reinit config JSON (secondary_nodes / primary_node) ──────
            _rc_cluster  = _mdata.get("cluster") or {}
            _rc_sec      = _mdata.get("secondary_nodes") or []
            manifest_nodes = [
                dict(
                    bmc              = _sn.get("bmc", ""),
                    bmc_user         = _sn.get("bmc_user") or "admin",
                    bmc_password     = _sn.get("bmc_password") or "",
                    node_mgmt_ip     = _sn.get("node_mgmt_ip") or "",
                    node_mgmt_port   = _sn.get("node_mgmt_port") or "e0M",
                    node_mgmt_netmask= _sn.get("node_mgmt_netmask") or "255.255.255.0",
                    node_mgmt_gateway= _sn.get("node_mgmt_gateway") or "",
                )
                for _sn in _rc_sec if _sn.get("bmc")
            ]
            cluster_mgmt_ip        = (_rc_cluster.get("clus_mgmt_address")
                                       or _rc_cluster.get("mgmt_ip") or "")
            cluster_admin_user     = _rc_cluster.get("user") or "admin"
            cluster_admin_password = _rc_cluster.get("password") or ""
            _manifest_source       = f"{manifest_path} (reconfig)"
            print(f"\n  Config   : {manifest_path}")
    else:
        # ── 2b. Try config-file secondary nodes ────────────────────────────
        _cfg_secondary = _config_secondary_nodes()
        _cfg_cluster   = (_config_data.get("cluster") or {}) if isinstance(_config_data, dict) else {}
        _cfg_mgmt_ip   = (_cluster_config.get("mgmt_ip")
                          or _cfg_cluster.get("clus_mgmt_address")
                          or _cfg_cluster.get("mgmt_ip") or "")
        if _cfg_secondary:
            print("\n  ℹ️  No manifest file found – using secondary nodes from the")
            print("       loaded config file as the node-add list.")
            manifest_nodes = [
                dict(
                    bmc              = _sn.get("bmc", ""),
                    bmc_user         = _sn.get("bmc_user") or "admin",
                    bmc_password     = _sn.get("bmc_password") or "",
                    node_mgmt_ip     = _sn.get("node_mgmt_ip") or "",
                    node_mgmt_port   = _sn.get("node_mgmt_port") or "e0M",
                    node_mgmt_netmask= _sn.get("node_mgmt_netmask") or "255.255.255.0",
                    node_mgmt_gateway= _sn.get("node_mgmt_gateway") or "",
                )
                for _sn in _cfg_secondary if _sn.get("bmc")
            ]
            cluster_mgmt_ip        = _cfg_mgmt_ip
            cluster_admin_user     = (_cluster_config.get("admin_user")
                                      or _cfg_cluster.get("user") or "admin")
            cluster_admin_password = (_cluster_config.get("admin_password")
                                      or _cfg_cluster.get("password") or "")
            _manifest_source       = "(config file)"
        else:
            # ── 2c. Last resort: ask operator for a path ───────────────────
            print("\n  No manifest file or config secondary nodes were found.")
            print("  Enter the path to a node-add manifest (created by option 3)")
            print("  or to the reinit config JSON file used during the original run.")
            manifest_path = _prompt("  Path: ")
            if not manifest_path or not os.path.isfile(manifest_path):
                print("  ❌ No manifest file and no secondary nodes in config. Aborting.")
                return False
            try:
                with open(manifest_path, "r", encoding="utf-8") as _mf:
                    _mdata = json.load(_mf)
            except Exception as _me:
                print(f"  ❌ Could not read file: {_me}")
                return False
            if "nodes" in _mdata and isinstance(_mdata["nodes"], list):
                # Standard node-add manifest
                manifest_nodes         = _mdata.get("nodes") or []
                cluster_mgmt_ip        = _mdata.get("cluster_mgmt_ip") or ""
                cluster_admin_user     = _mdata.get("cluster_admin_user") or "admin"
                cluster_admin_password = _mdata.get("cluster_admin_password") or ""
                _manifest_source       = manifest_path
                print(f"\n  Manifest : {manifest_path}")
                print(f"  Created  : {_mdata.get('created_at', 'unknown')}")
            else:
                # Reinit config JSON (secondary_nodes / primary_node)
                _rc_cluster2  = _mdata.get("cluster") or {}
                _rc_sec2      = _mdata.get("secondary_nodes") or []
                manifest_nodes = [
                    dict(
                        bmc              = _sn.get("bmc", ""),
                        bmc_user         = _sn.get("bmc_user") or "admin",
                        bmc_password     = _sn.get("bmc_password") or "",
                        node_mgmt_ip     = _sn.get("node_mgmt_ip") or "",
                        node_mgmt_port   = _sn.get("node_mgmt_port") or "e0M",
                        node_mgmt_netmask= _sn.get("node_mgmt_netmask") or "255.255.255.0",
                        node_mgmt_gateway= _sn.get("node_mgmt_gateway") or "",
                    )
                    for _sn in _rc_sec2 if _sn.get("bmc")
                ]
                cluster_mgmt_ip        = (_rc_cluster2.get("clus_mgmt_address")
                                           or _rc_cluster2.get("mgmt_ip") or "")
                cluster_admin_user     = _rc_cluster2.get("user") or "admin"
                cluster_admin_password = _rc_cluster2.get("password") or ""
                _manifest_source       = f"{manifest_path} (reconfig)"
                print(f"\n  Config   : {manifest_path}")

    if not manifest_nodes:
        print("  ❌ No nodes found in manifest or config. Aborting.")
        return False

    # ── 2. Display node list ──────────────────────────────────────────────
    print(f"  Source   : {_manifest_source}")
    print(f"  Nodes    : {len(manifest_nodes)}")
    for _n in manifest_nodes:
        _bmc = _n.get("bmc", "?")
        _nip = _n.get("node_mgmt_ip") or "(unknown)"
        print(f"    - BMC {_bmc}  node-mgmt IP {_nip}")

    # ── 3. Connect to cluster management ─────────────────────────────────
    if not cluster_mgmt_ip:
        # Last-chance fallback 1: in-memory config data.
        _raw_cfg_cl = (_config_data.get("cluster") or {}) if isinstance(_config_data, dict) else {}
        cluster_mgmt_ip = (_raw_cfg_cl.get("clus_mgmt_address")
                           or _raw_cfg_cl.get("mgmt_ip") or "")
    if not cluster_mgmt_ip:
        # Last-chance fallback 2: scan script dir + configs/ for any reconfig JSON.
        for _scan_dir in (script_dir, configs_dir):
            if not os.path.isdir(_scan_dir):
                continue
            for _cp2 in sorted(_glob.glob(os.path.join(_scan_dir, "*.json")), reverse=True):
                try:
                    with open(_cp2, "r", encoding="utf-8") as _cf2:
                        _cj2 = json.load(_cf2)
                    _ip2 = (_cj2.get("cluster") or {}).get("clus_mgmt_address") or ""
                    if _ip2:
                        cluster_mgmt_ip = _ip2
                        print(f"  ℹ️  Cluster mgmt IP {_ip2} read from {_cp2}")
                        break
                except Exception:
                    pass
            if cluster_mgmt_ip:
                break
    if not cluster_mgmt_ip:
        cluster_mgmt_ip = _prompt(
            "\n  Cluster management IP: "
        )
    if not cluster_mgmt_ip:
        print("  ❌ No cluster management IP provided. Aborting.")
        return False

    if not cluster_admin_password:
        try:
            cluster_admin_password = getpass.getpass(
                f"  Cluster admin password for {cluster_admin_user}@"
                f"{cluster_mgmt_ip}: "
            )
        except (EOFError, KeyboardInterrupt):
            cluster_admin_password = ""

    print(f"\n  🔌 Connecting to cluster {cluster_mgmt_ip}...")
    primary_client  = None
    primary_channel = None
    cluster_node_ips: dict = {}   # {node_name: mgmt_ip}

    while True:
        try:
            _cu = cluster_admin_user
            _cp = cluster_admin_password
            primary_client, _cu, _cp = _ssh_connect_with_retry(
                cluster_mgmt_ip, _cu, _cp,
                label=f"2c/{cluster_mgmt_ip}",
                max_attempts=1, interactive=False,
            )
            cluster_admin_user    = _cu
            cluster_admin_password = _cp
            _pch = _open_shell(primary_client)
            if _login_primary_cluster_shell(_pch, cluster_admin_password):
                primary_channel = _pch
                print("  ✅ Connected to cluster shell.")
                _session_log.log(f"2c: cluster shell connected to {cluster_mgmt_ip}")
            else:
                primary_client.close()
                primary_client = None
                print("  ⚠️  Cluster shell login failed.")
                _session_log.log("2c: cluster shell login failed", prefix="WARN")
                _ask_skip = _prompt(
                    "  Continue without cluster comparison (retry all nodes)? [y/N]: "
                , "n").lower()
                if _ask_skip != "y":
                    return False
            break
        except paramiko.AuthenticationException:
            print(f"  ❌ Authentication failed for {cluster_admin_user}@{cluster_mgmt_ip}.")
            _session_log.log(
                f"2c: auth failed for {cluster_admin_user}@{cluster_mgmt_ip}",
                prefix="WARN",
            )
            try:
                cluster_admin_user = (
                    input(f"    Username [{cluster_admin_user}]: ").strip()
                    or cluster_admin_user
                )
                cluster_admin_password = getpass.getpass(
                    f"    Password for {cluster_admin_user}@{cluster_mgmt_ip}: "
                )
            except (EOFError, KeyboardInterrupt):
                cluster_admin_password = ""
            if not cluster_admin_password:
                print("  Aborting — no credentials provided.")
                return False
        except Exception as _ce:
            print(f"  ❌ Connection failed: {_ce}")
            _session_log.log(f"2c: connection to {cluster_mgmt_ip} failed: {_ce}",
                             prefix="ERROR")
            return False

    # ── 4. Determine which nodes are already in the cluster ───────────────
    if primary_channel:
        print("\n  🔍 Querying cluster for node-mgmt IPs...")
        # Derive cluster name from config data so the single net int show
        # command can use it as the vserver filter.
        _c_name = ((_config_data.get("cluster") or {}).get("name")
                   if isinstance(_config_data, dict) else None)
        cluster_node_ips = _get_cluster_node_mgmt_ips(primary_channel,
                                                       cluster_name=_c_name)
        if cluster_node_ips:
            print(f"  Found {len(cluster_node_ips)} node(s) in cluster:")
            for _nn, _nip in sorted(cluster_node_ips.items()):
                print(f"    ✅ {_nn}: {_nip}")
            _session_log.log(
                f"2c: cluster nodes: {dict(cluster_node_ips)}"
            )
        else:
            print("  ⚠️  No node-mgmt IPs found in cluster.")
            _session_log.log("2c: net int show returned no node-mgmt IPs", prefix="WARN")

    # ── 5. Compare manifest vs cluster ───────────────────────────────────
    already_joined_ips = set(cluster_node_ips.values())
    nodes_to_retry  = []
    nodes_already   = []
    for _nd in manifest_nodes:
        _nip = (_nd.get("node_mgmt_ip") or "").strip()
        if _nip and _nip in already_joined_ips:
            nodes_already.append(_nd)
        else:
            nodes_to_retry.append(_nd)

    print()
    if nodes_already:
        print(f"  Already joined ({len(nodes_already)}):")
        for _nd in nodes_already:
            print(f"    ✅ BMC {_nd.get('bmc')}  "
                  f"(node-mgmt {_nd.get('node_mgmt_ip') or '?'})")
    if nodes_to_retry:
        print(f"\n  To retry ({len(nodes_to_retry)}):")
        for _nd in nodes_to_retry:
            print(f"    ⏳ BMC {_nd.get('bmc')}  "
                  f"(node-mgmt {_nd.get('node_mgmt_ip') or '?'})")

    if not nodes_to_retry:
        print("\n  ✅ All manifest nodes are already in the cluster. Nothing to do.")
        _session_log.log("2c: all manifest nodes already joined; nothing to retry")
        if primary_client:
            try:
                primary_client.close()
            except Exception:
                pass
        _session_log.set_outcome("PASS", "all nodes already in cluster")
        return True

    # ── 6. Confirm ────────────────────────────────────────────────────────
    _ans_retry = _prompt(
        f"\n  Proceed with retrying {len(nodes_to_retry)} node(s)? [y/N]: "
    , "n").lower()
    if _ans_retry != "y":
        print("  Aborting.")
        _session_log.log("2c: operator declined retry")
        if primary_client:
            try:
                primary_client.close()
            except Exception:
                pass
        return False

    # ── 7. Seed in-memory state from manifest and launch threads ──────────
    # Populate credential + node-mgmt caches so _add_peer_node_thread can
    # look up the same data it would have from a fresh 2b run.
    _cluster_config["mgmt_ip"]          = cluster_mgmt_ip
    _cluster_config["admin_user"]        = cluster_admin_user
    _cluster_config["admin_password"]    = cluster_admin_password

    retry_bmc_list = []
    for _nd in nodes_to_retry:
        _bmc = (_nd.get("bmc") or "").strip()
        if not _bmc:
            continue
        _bu = _nd.get("bmc_user") or "admin"
        _bp = _nd.get("bmc_password") or ""
        if not _bp:
            try:
                _bp = getpass.getpass(f"  BMC password for {_bu}@{_bmc}: ")
            except (EOFError, KeyboardInterrupt):
                _bp = ""
        _peer_bmc_creds[_bmc] = {"user": _bu, "password": _bp}
        if _nd.get("node_mgmt_ip"):
            _node_mgmt_by_bmc[_bmc] = {
                "node_mgmt_port":    _nd.get("node_mgmt_port")    or "e0M",
                "node_mgmt_ip":      _nd.get("node_mgmt_ip"),
                "node_mgmt_netmask": _nd.get("node_mgmt_netmask") or "255.255.255.0",
                "node_mgmt_gateway": _nd.get("node_mgmt_gateway") or "",
            }
        retry_bmc_list.append(_bmc)
        _session_log.log(f"2c: will retry BMC {_bmc} (user={_bu})")

    if not retry_bmc_list:
        print("  ❌ No valid BMC IPs to retry. Aborting.")
        _session_log.log("2c: no valid BMCs to retry", prefix="ERROR")
        if primary_client:
            try:
                primary_client.close()
            except Exception:
                pass
        return False

    baseline      = len(cluster_node_ips) if primary_channel else 0
    _n2c          = len(retry_bmc_list)
    _join_barrier = threading.Barrier(_n2c)
    _join_proceed = [threading.Event() for _ in range(_n2c)]
    _join_proceed[0].set()

    _session_log.start_phase("2c – Resume Node Add")
    threads = []
    for _idx, _bmc in enumerate(retry_bmc_list):
        _creds = _peer_bmc_creds.get(_bmc) or {}
        _u = _creds.get("user") or "admin"
        _p = _creds.get("password") or ""
        _expected  = baseline + _idx + 1 if primary_channel else 0
        _final     = baseline + _n2c      if primary_channel else 0
        _t = threading.Thread(
            target=_add_peer_node_thread,
            args=(_bmc, _u, _p, primary_channel,
                  cluster_admin_password, _expected,
                  _join_barrier, _join_proceed, _idx),
            kwargs={"final_cluster_count": _final},
            daemon=True,
            name=f"2c-resume-{_bmc}",
        )
        _t.start()
        threads.append(_t)
        print(f"  ▶️  [{_bmc}] Thread started.")

    print(f"\n  ⏳ Waiting for {_n2c} node(s) to complete...")
    for _t in threads:
        _t.join()

    _session_log.end_phase()
    print("\n  ✅ 2c resume complete.")
    _session_log.log("2c resume: all threads finished")
    _session_log.set_outcome("PASS", "2c resume complete")

    if primary_client:
        try:
            primary_client.close()
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _operation_mode, _auto_setup, _auto_add, _config_data
    global _add_another_node_request, _bg_mode, _debug_console
    global _primary_bmc_user, _primary_bmc_password
    global _checkpoint, _run_context
    global _auto_clear_stale_bmc

    args = parse_args()

    # --screen: re-exec inside a GNU screen session for connection resilience.
    if args.screen and _relaunch_in_screen():
        sys.exit(0)

    setup_logging(args.debug)
    _debug_console = args.debug
    if _debug_console:
        print("🔍 Debug mode enabled: all console output will be shown on screen.")

    if args.config_example:
        print(_CONFIG_FILE_EXAMPLE)
        sys.exit(0)

    if args.checkpoint_status:
        _cp = CheckpointManager()
        if not _cp.load():
            print(f"No valid checkpoint found at {_cp._path}.")
            sys.exit(1)
        print(_cp.summary())
        sys.exit(0)

    _bg_mode = args.bg
    _auto_clear_stale_bmc = args.auto_clear_stale_bmc
    if _auto_clear_stale_bmc:
        print("🧹 --auto-clear-stale-bmc enabled: banner retries will SIGTERM "
              "stale prior-run python PIDs holding sockets to <bmc>:22.")

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    # In background mode, handle SIGHUP (terminal close) gracefully so the log
    # is flushed and closed cleanly rather than truncated mid-write.
    if _bg_mode and hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal_handler)

    # ── --resume: auto-dispatch from checkpoint, skip the start menu ──────
    # When --resume is explicit and a valid checkpoint exists, infer the
    # operation mode from the checkpoint and bypass select_operation_mode()
    # so the operator does not have to re-pick the same menu option.
    _resume_autodispatch = False
    _resume_cp: "CheckpointManager | None" = None
    if args.resume:
        _resume_cp = CheckpointManager()
        if _resume_cp.load():
            _cp_mode = (_resume_cp.mode or "").strip()
            if _cp_mode.startswith("4b"):
                # Mode 4b (optionally with a reinit sub-mode like "4b-3").
                _operation_mode = 42
                _auto_setup = False
                _auto_add = False
                _resume_autodispatch = True
                print(f"\n  🔖 --resume: auto-dispatching to mode 4b "
                      f"from checkpoint (mode={_cp_mode}).")
            else:
                print(f"\n  ⚠️  --resume: checkpoint mode {_cp_mode!r} cannot "
                      "be auto-dispatched; falling back to the start menu.")
                _resume_cp = None
        else:
            print(f"\n  ⚠️  --resume: no valid checkpoint found at "
                  f"{_resume_cp._path}; falling back to the start menu.")
            _resume_cp = None

    if not _resume_autodispatch:
        _operation_mode, _auto_setup, _auto_add = select_operation_mode()
    # Remember the mode the operator explicitly chose at startup.  Mid-run
    # transitions (e.g. 1b → add nodes) change _operation_mode but leave
    # _initial_operation_mode intact so mode-specific up-front prompts only
    # fire when the operator actually started in that mode.
    _initial_operation_mode = _operation_mode

    # ── RunContext: snapshot current globals into a single state object ──
    # Phase 2 of the RunContext refactor. The context is the migration
    # target for the heavy globals listed in _RUN_CONTEXT_FIELD_TO_GLOBAL;
    # legacy code paths still read/write the globals directly. Container
    # fields (dicts/lists) share identity with their globals so in-place
    # mutations stay visible through both views during the migration.
    # Migrated helpers will mutate _run_context and call apply_to_globals()
    # at the end so any downstream legacy code still sees fresh values.
    _run_context = RunContext.from_globals()

    # Mode 4 (4b/4c): not yet implemented placeholders.
    if _operation_mode == 4:
        _print_banner("\U0001f4e6 This ONTAP install sub-option is not yet implemented.")
        print("  Please check back in a future release.")
        sys.exit(0)

    # ── Mode 26 (2c): Resume interrupted node additions ───────────────────
    if _operation_mode == 26:
        ok = _run_2c_resume()
        if _session_log:
            _session_log.record_completion(normal_exit=ok)
            print(f"\n📝 Session log saved to: {_session_log.log_file}")
        sys.exit(0 if ok else 1)

    # ── Mode 42 (4b): Netboot and install ONTAP ────────────────────────────
    if _operation_mode == 42:
        # ── Resume detection ──────────────────────────────────────────────
        # Reuse the checkpoint already loaded by the --resume auto-dispatch
        # path so we do not re-read the file or re-prompt for confirmation.
        _cp = _resume_cp or CheckpointManager()
        _resuming = False
        if _resume_autodispatch and _resume_cp is not None:
            # --resume + valid checkpoint: skip the confirmation prompt and
            # resume directly. Operator already opted in via the CLI flag.
            _resuming = True
            _checkpoint = _cp
            print("\n  ✅ Resuming from checkpoint (--resume).")
        elif args.resume or (not args.resume and _cp.load()):
            # If --resume was explicit, or a checkpoint file auto-detected,
            # confirm with the operator before using it.
            if _cp.load():
                _cp_age = ""
                try:
                    _cp_dt = datetime.fromisoformat(_cp.created)
                    _cp_mins = int((datetime.now() - _cp_dt).total_seconds() // 60)
                    _cp_age = f" from {_cp_mins} minute(s) ago"
                except Exception:
                    pass
                _install_done_ips = _cp.nodes_done_for("install_done")
                _reinit_done_ips  = _cp.nodes_done_for("reinit_loader")
                _opt4_done_ips    = _cp.nodes_done_for("peer_option4_done")
                _joined_ips       = _cp.nodes_done_for("peer_joined")
                print("\n" + "=" * 60)
                print("  🔖 Checkpoint found" + _cp_age)
                print(f"     Mode    : {_cp.mode}")
                print(f"     BMC IPs : {', '.join(_cp.bmc_ips)}")
                print(f"     Log dir : {_cp.log_dir}")
                if _install_done_ips:
                    print(f"     install_done          : {', '.join(_install_done_ips)}")
                if _reinit_done_ips:
                    print(f"     reinit_loader         : {', '.join(_reinit_done_ips)}")
                if _opt4_done_ips:
                    print(f"     peer_option4_done     : {', '.join(_opt4_done_ips)}")
                if _joined_ips:
                    print(f"     peer_joined           : {', '.join(_joined_ips)}")
                if _cp.is_done("primary_bootmenu_done"):
                    print("     primary_bootmenu_done : ✅")
                if _cp.is_done("cluster_formed"):
                    print("     cluster_formed        : ✅")
                if _cp.is_done("primary_setup_done"):
                    print("     primary_setup_done    : ✅")
                if _cp.is_done("option3_complete"):
                    print("     option3_complete      : ✅")
                print("=" * 60)
                _resume_ans = _prompt(
                    "\n  Resume from checkpoint? [Y/n]: "
                , "n").lower()
                if _resume_ans != "n":
                    _resuming = True
                    _checkpoint = _cp
                    print("  ✅ Resuming from checkpoint.")
                else:
                    _cp.clear()
                    print("  ℹ️  Starting fresh run.")
        if not _resuming:
            _checkpoint = CheckpointManager()

        _make_session_log("Mode 42: netboot and install ONTAP (4b)")
        ok = _run_4b_standalone(_session_log, resuming=_resuming)
        _session_log.record_completion(normal_exit=ok)
        if ok:
            _checkpoint.clear()
        print(f"\n\U0001f4dd Session log saved to: {_session_log.log_file}")
        sys.exit(0 if ok else 1)

    # ── Mode 41 (4a): ONTAP upgrade ────────────────────────────────────────
    if _operation_mode == 41:
        _make_session_log("Mode 41: ONTAP upgrade (rolling takeover/giveback)")
        ok = _run_ontap_upgrade(_session_log)
        _session_log.record_completion(normal_exit=ok)
        print(f"\n\U0001f4dd Session log saved to: {_session_log.log_file}")
        sys.exit(0 if ok else 1)

    # ── Mode 44 (4c): standalone license-only install ──────────────────────
    if _operation_mode == 44:
        _collect_license_config(_run_context)
        if not _license_mode:
            print("\n  No license configured; nothing to do. Exiting.")
            sys.exit(0)

        # Optional config file for BMC credentials.
        config_path_44 = args.config
        if not config_path_44:
            _found_44 = _find_config_files(
                candidate_names=("reinit-config.json", "reinit_config.json",
                                 "reinit-afx-config.json", "reinit_afx_config.json",
                                 "config.json"),
            )
            if _found_44:
                config_path_44 = _found_44[0]
        if config_path_44:
            try:
                _config_data = load_config_file(config_path_44)
                print(f"\U0001f4c4 Loaded config: {config_path_44}")
            except ValueError:
                _config_data = {}
        else:
            _config_data = {}
        _ctx_sync_from_globals()

        _make_session_log("Mode 4c: standalone license install")

        # BMC credentials from config or interactive prompts.
        primary_node_44 = {}
        if isinstance(_config_data, dict):
            nl = _config_data.get("nodes") or []
            if nl and isinstance(nl[0], dict):
                primary_node_44 = nl[0]

        sp_host = _cfg_str(primary_node_44.get("bmc"))
        if sp_host:
            _check_bmc_reachable(sp_host)
        else:
            sp_host = _prompt_bmc_host(
                "  Enter BMC hostname/IP or cluster management IP address: "
            )
        sp_user = _cfg_str(primary_node_44.get("bmc_user")) or input("  BMC username: ").strip()
        if "bmc_password" in primary_node_44 and isinstance(primary_node_44["bmc_password"], str):
            sp_pass = primary_node_44["bmc_password"]
        else:
            sp_pass = getpass.getpass("  BMC password: ")

        _session_log.log(f"Target BMC: {sp_host} (user={sp_user})")

        # Connect to BMC.
        _session_log.start_phase("SSH Connection")
        client_44, sp_user, sp_pass = connect_to_sp(sp_host, sp_user, sp_pass)
        channel_44 = _open_shell(client_44)
        keepalive_thread_44 = threading.Thread(
            target=keepalive_loop, args=(client_44,), daemon=True
        )
        keepalive_thread_44.start()
        _session_log.end_phase()

        if not wait_for_bmc_prompt(channel_44, auto_takeover=True):
            print("\n  \u274c BMC prompt not received. Exiting.")
            _session_log.set_outcome("FAIL", "BMC prompt not received")
            _session_log.close()
            sys.exit(1)

        # Enter system console and login to cluster shell.
        _session_log.start_phase("Cluster Shell Login")
        enter_system_console(channel_44)
        if not _wait_for_cluster_prompt(channel_44, timeout=60):
            print("\n  \u26a0\ufe0f  Cluster prompt not detected; trying admin login...")
            admin_pw_44 = _cluster_config.get("admin_password") or ""
            if not admin_pw_44:
                admin_pw_44 = getpass.getpass("  Cluster admin password: ")
            if not _login_primary_cluster_shell(channel_44, admin_pw_44):
                print("  \u274c Cluster shell login failed. Exiting.")
                _session_log.set_outcome("FAIL", "Cluster shell login failed")
                _session_log.close()
                sys.exit(1)
        _session_log.end_phase()

        # Apply license(s).
        _apply_license(channel_44)

        try:
            channel_44.close()
        except Exception:
            pass
        try:
            client_44.close()
        except Exception:
            pass

        print("\n\U0001f512 SSH session closed.")
        _session_log.record_completion(normal_exit=True)
        print(f"\n\U0001f4dd Full session log saved to: {_session_log.log_file}")
        sys.exit(0)

    # ── Mode 46 (4e): create backup cluster configuration ──────────────────
    if _operation_mode == 46:
        _print_banner("\U0001f4be 4e: Create backup cluster configuration")
        print("")
        _make_session_log("Mode 4e: backup cluster configuration")

        # Resolve output dir early — needed throughout.
        try:
            _snap_dir46 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")
        except NameError:
            _snap_dir46 = os.path.join(os.getcwd(), "configs")
        os.makedirs(_snap_dir46, exist_ok=True)

        # ── Top-level: gather from cluster vs. build manually ─────────────
        print("  How would you like to build the configuration file?\n")
        print("    gather  - Connect to an existing cluster and read its config")
        print("    build   - Enter the configuration manually\n")
        while True:
            _mode46 = _prompt("  Your choice [gather/build]: ").lower()
            if _mode46 in ("gather", "build"):
                break
            print("  ⚠️  Please enter 'gather' or 'build'.")
        _session_log.log(f"4e choice: {_mode46}")

        # ── GATHER PATH: connect to cluster BMC ─────────────────────────
        if _mode46 == "gather":
            print("")
            print("  ⚠️  Backup configuration will only work on a cluster")
            print("       that has an existing configuration.")
            print("")

            # Optional config file for BMC credentials.
            _cfg46 = {}
            _found_46 = _find_config_files(
                candidate_names=("reinit-config.json", "reinit_config.json",
                                 "reinit-afx-config.json", "reinit_afx_config.json",
                                 "config.json"),
            )
            if _found_46:
                _c46 = _found_46[0]
                try:
                    _cfg46 = load_config_file(_c46)
                    print(f"  \U0001f4c4 Loaded config: {_c46}")
                except ValueError:
                    _cfg46 = {}

            # Resolve primary node block from new or legacy format.
            _pn46 = (_cfg46.get("primary_node")
                     if isinstance(_cfg46.get("primary_node"), dict)
                     else ((_cfg46.get("nodes") or [None])[0]
                           if isinstance(_cfg46.get("nodes"), list) else None)) or {}

            sp_host46 = _cfg_str(_pn46.get("bmc"))
            if sp_host46:
                _check_bmc_reachable(sp_host46)
            else:
                sp_host46 = _prompt_bmc_host(allow_blank=True)
            if not sp_host46:
                print("  No BMC address entered. Exiting.")
                sys.exit(0)
            sp_user46 = _cfg_str(_pn46.get("bmc_user")) or input("  BMC username [admin]: ").strip() or "admin"
            if "bmc_password" in _pn46 and isinstance(_pn46["bmc_password"], str):
                sp_pass46 = _pn46["bmc_password"]
            else:
                sp_pass46 = getpass.getpass("  BMC password (blank = none): ")

            _session_log.log(f"Target BMC: {sp_host46} (user={sp_user46})")

            # Connect to BMC – no interactive re-prompt; auth failure exits immediately.
            _session_log.start_phase("SSH Connection")
            try:
                _client46, sp_user46, sp_pass46 = _ssh_connect_with_retry(
                    sp_host46, sp_user46, sp_pass46,
                    label=f"BMC/{sp_host46}", max_attempts=1, interactive=False,
                )
            except Exception as _e46:
                print(f"  \u274c SSH connection failed: {_e46}")
                print("  Check credentials and try again.")
                _session_log.set_outcome("FAIL", f"SSH failed: {_e46}")
                _session_log.close()
                sys.exit(1)

            # Make the 4e credentials available to the cluster-login fallback chain
            # so that collect_retain_data can log in silently without re-prompting.
            _primary_bmc_user = sp_user46
            _primary_bmc_password = sp_pass46
            if not _cluster_config.get("admin_user"):
                _cluster_config["admin_user"] = sp_user46
            if not _cluster_config.get("admin_password"):
                _cluster_config["admin_password"] = sp_pass46
            _ch46 = _open_shell(_client46)
            _kt46 = threading.Thread(target=keepalive_loop, args=(_client46,), daemon=True)
            _kt46.start()
            _session_log.end_phase()

            if not wait_for_bmc_prompt(_ch46, auto_takeover=True):
                print("  \u274c BMC prompt not received. Exiting.")
                _session_log.set_outcome("FAIL", "BMC prompt not received")
                _session_log.close()
                sys.exit(1)

            # Run the full retain capture (name + network + peer SPs).
            _session_log.start_phase("Cluster Inventory Capture")
            _cname46r, _net46, _peers46 = collect_retain_data(
                _ch46,
                retain_name=True,
                retain_network=True,
                collect_peer_sps=True,
            )
            _session_log.end_phase()

            try:
                _ch46.close()
            except Exception:
                pass
            try:
                _client46.close()
            except Exception:
                pass

            # Merge all captured data into _config_data.
            _config_data = dict(_cfg46)  # start from any file values
            _ctx_sync_from_globals()
            apply_retained_to_cluster_config()
            apply_retained_to_node_configs(primary_bmc=sp_host46)
            # _peers46 holds the SP/BMC IPs from 'service-processor show'.
            _sp_ips46 = list(_peers46) if _peers46 else []
            if _sp_ips46:
                print(f"  \U0001f4cb SP/BMC addresses from cluster: {', '.join(_sp_ips46)}")
                _session_log.log(f"SP IPs from cluster: {_sp_ips46}")

        # ── BUILD PATH: manual entry ─────────────────────────────────────
        else:
            print("")
            print("  Create a new cluster configuration or add nodes to an existing one?\n")
            print("    create  - Enter all cluster details from scratch")
            print("    add     - Connect to a cluster and list new nodes to add\n")
            while True:
                _build46 = _prompt("  Your choice [create/add]: ").lower()
                if _build46 in ("create", "add"):
                    break
                print("  ⚠️  Please enter 'create' or 'add'.")
            _session_log.log(f"4e build sub-choice: {_build46}")

            _config_data = {}

            if _build46 == "create":
                # ── Cluster-level details ──────────────────────────────────
                print("\n" + "─" * 60)
                print("  Cluster configuration")
                print("─" * 60)
                _b_name  = input("  Cluster name: ").strip()
                _b_mport = input("  Cluster management interface port [e0M]: ").strip() or "e0M"
                _b_mip   = input("  Cluster management IP address: ").strip()
                _b_mmask = input("  Cluster management netmask: ").strip()
                _b_mgw   = input("  Cluster management gateway: ").strip()
                _b_dns_d = input("  DNS domain names (comma separated): ").strip()
                _b_dns_s = input("  DNS servers (comma separated): ").strip()
                _b_loc   = input("  Controller location (optional): ").strip()
                _b_pw    = getpass.getpass("  Cluster admin password: ")

                _config_data["cluster"] = {
                    "name": _b_name,
                    "clus_mgmt_port": _b_mport,
                    "clus_mgmt_address": _b_mip,
                    "clus_mgmt_mask": _b_mmask,
                    "clus_mgmt_gw": _b_mgw,
                    "dns_domains": _b_dns_d,
                    "dns_servers": _b_dns_s,
                    "location": _b_loc,
                    "password": _b_pw,
                }

                # ── Primary node ───────────────────────────────────────────
                print("\n" + "─" * 60)
                print("  Primary node BMC")
                print("─" * 60)
                _b_pbmc = _prompt_bmc_host()
                _b_puser = input("  BMC username [admin]: ").strip() or "admin"
                _b_ppw   = getpass.getpass("  BMC password (blank = none): ")
                _b_pport = input("  Node management port [e0M]: ").strip() or "e0M"
                _b_pip   = input("  Node management IP: ").strip()
                _b_pmask = input("  Node management netmask: ").strip()
                _b_pgw   = input("  Node management gateway: ").strip()
                _config_data["primary_node"] = {
                    "bmc": _b_pbmc,
                    "bmc_user": _b_puser,
                    "bmc_password": _b_ppw,
                    "node_mgmt_port": _b_pport,
                    "node_mgmt_ip": _b_pip,
                    "node_mgmt_netmask": _b_pmask,
                    "node_mgmt_gateway": _b_pgw,
                }
                _config_data["secondary_nodes"] = []
                _session_log.log("4e build/create: manual cluster config collected")
                _sp_ips46 = []  # No cluster shell in create path; no SP addresses available.

            else:
                # ── ADD PATH ─────────────────────────────────────────────
                # Goal: patch cluster_network_ip into an existing config file
                # and append the new nodes to secondary_nodes so the file can
                # immediately drive option 2a/2b.
                # We only need the cluster shell to get a cluster-network IP;
                # we do NOT call collect_retain_data so primary_node and all
                # cluster fields are preserved exactly as-is.

                # 1. Load an existing config file (same search as gather path).
                print("")
                _cfg46_add = {}
                _cfg46_add_path = None
                _found_46a = _find_config_files(
                    candidate_names=("reinit-config.json", "reinit_config.json",
                                     "reinit-afx-config.json", "reinit_afx_config.json",
                                     "config.json"),
                )
                if _found_46a:
                    _c46a = _found_46a[0]
                    try:
                        _cfg46_add = load_config_file(_c46a)
                        _cfg46_add_path = _c46a
                        print(f"  \U0001f4c4 Loaded existing config: {_c46a}")
                    except ValueError:
                        _cfg46_add = {}

                if not _cfg46_add_path:
                    print("  \u2139\ufe0f  No existing config file found. A new one will be created.")

                # Start _config_data from whatever was loaded (preserves all
                # existing sections: cluster, primary_node, secondary_nodes …).
                _config_data = dict(_cfg46_add)
                _ctx_sync_from_globals()

                # 2. Resolve the primary node BMC from config (or prompt).
                _pn46a = (_config_data.get("primary_node")
                          if isinstance(_config_data.get("primary_node"), dict)
                          else ((_config_data.get("nodes") or [None])[0]
                                if isinstance(_config_data.get("nodes"), list)
                                else None)) or {}

                print("\n" + "─" * 60)
                print("  Primary node BMC  (existing cluster)")
                print("─" * 60)
                _b_pbmc_default = _cfg_str(_pn46a.get("bmc"))
                if _b_pbmc_default:
                    print(f"  \U0001f4c4 BMC from config: {_b_pbmc_default}")
                    _b_pbmc = _b_pbmc_default
                    _check_bmc_reachable(_b_pbmc)
                else:
                    _b_pbmc = _prompt_bmc_host(allow_blank=True)
                if not _b_pbmc:
                    print("  No BMC address entered. Exiting.")
                    sys.exit(0)

                _b_puser_default = _cfg_str(_pn46a.get("bmc_user"))
                if _b_puser_default:
                    print(f"  \U0001f4c4 BMC username from config: {_b_puser_default}")
                    _b_puser = _b_puser_default
                else:
                    _b_puser = input("  BMC username [admin]: ").strip() or "admin"

                if "bmc_password" in _pn46a and isinstance(_pn46a["bmc_password"], str):
                    _b_ppw = _pn46a["bmc_password"]
                    print("  \U0001f4c4 BMC password from config.")
                else:
                    _b_ppw = getpass.getpass("  BMC password (blank = none): ")

                _session_log.log(f"4e build/add: connecting to BMC {_b_pbmc} (user={_b_puser})")
                _session_log.start_phase("SSH Connection (primary BMC)")
                try:
                    _bclient46, _b_puser, _b_ppw = _ssh_connect_with_retry(
                        _b_pbmc, _b_puser, _b_ppw,
                        label=f"BMC/{_b_pbmc}", max_attempts=1, interactive=False,
                    )
                except Exception as _be46:
                    print(f"  \u274c SSH connection failed: {_be46}")
                    print("  Check credentials and try again.")
                    _session_log.set_outcome("FAIL", f"SSH failed: {_be46}")
                    _session_log.close()
                    sys.exit(1)

                _primary_bmc_user = _b_puser
                _primary_bmc_password = _b_ppw
                if not _cluster_config.get("admin_password"):
                    _cluster_config["admin_password"] = _b_ppw

                _bch46 = _open_shell(_bclient46)
                _bkt46 = threading.Thread(target=keepalive_loop, args=(_bclient46,), daemon=True)
                _bkt46.start()
                _session_log.end_phase()

                if not wait_for_bmc_prompt(_bch46, auto_takeover=True):
                    print("  \u274c BMC prompt not received. Exiting.")
                    _session_log.set_outcome("FAIL", "BMC prompt not received")
                    _session_log.close()
                    sys.exit(1)

                # 3. Enter cluster shell and query the cluster-network IP.
                _session_log.start_phase("Cluster Shell (cluster-network IP)")
                enter_system_console(_bch46)
                if not _wait_for_cluster_prompt(_bch46, timeout=60):
                    if not _attempt_console_cluster_login(_bch46):
                        print("  \u274c Could not log into cluster shell. Exiting.")
                        _session_log.set_outcome("FAIL", "cluster shell login failed")
                        _session_log.close()
                        sys.exit(1)

                print("\n  \U0001f50d Querying cluster-network interfaces...")
                _bch46.send("net int show -role cluster -fields address\r")
                _clus_out46 = direct_read_until(_bch46, "::", timeout=30)
                _clus_ip46 = None
                for _cline46 in _clus_out46.splitlines():
                    _cm46 = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', _cline46)
                    if _cm46:
                        _clus_ip46 = _cm46.group(1)
                        break
                if _clus_ip46:
                    print(f"  \u2705 Cluster-network IP found: {_clus_ip46}")
                    _session_log.log(f"Cluster-network IP: {_clus_ip46}")
                else:
                    print("  \u26a0\ufe0f  Could not detect cluster-network IP automatically.")
                    try:
                        _clus_ip46 = input(
                            "  Enter cluster-network IP manually (or blank to skip): "
                        ).strip() or None
                    except (EOFError, KeyboardInterrupt):
                        _clus_ip46 = None

                _session_log.end_phase()

                try:
                    _bch46.close()
                except Exception:
                    pass
                try:
                    _bclient46.close()
                except Exception:
                    pass

                # 4. Patch cluster_network_ip into primary_node — preserve all
                #    other existing fields; create a minimal stub only if needed.
                if not isinstance(_config_data.get("primary_node"), dict):
                    _config_data["primary_node"] = {}
                if _clus_ip46:
                    _config_data["primary_node"]["cluster_network_ip"] = _clus_ip46

                # 5. Prompt for new node BMC addresses (nodes to be joined).
                print("\n" + "─" * 60)
                print("  Nodes to join to the cluster (not yet joined)")
                print("  Enter BMC details for each new node; blank BMC IP to finish.")
                print("─" * 60)
                if not isinstance(_config_data.get("secondary_nodes"), list):
                    _config_data["secondary_nodes"] = []
                _nadd_idx46 = 1
                while True:
                    _nadd_bmc46 = _prompt(f"\n  Node {_nadd_idx46} BMC hostname/IP (blank to finish): ")
                    if not _nadd_bmc46:
                        break
                    _check_bmc_reachable(_nadd_bmc46)
                    def _prompt_ip46(label):
                        """Prompt until a non-blank string is entered. Ctrl+C raises."""
                        while True:
                            val = input(f"  {label}: ").strip()
                            if val:
                                return val
                            print("    ⚠️  Value cannot be blank. Press Ctrl+C to cancel this node.")
                    try:
                        _nadd_user46 = input(f"  Node {_nadd_idx46} BMC username [admin]: ").strip() or "admin"
                        _nadd_pw46   = getpass.getpass(f"  Node {_nadd_idx46} BMC password (blank = none): ")
                        _nadd_port46 = input(f"  Node {_nadd_idx46} mgmt port [e0M]: ").strip() or "e0M"
                        _nadd_ip46   = _prompt_ip46(f"Node {_nadd_idx46} mgmt IP")
                        _nadd_mask46 = _prompt_ip46(f"Node {_nadd_idx46} mgmt netmask")
                        _nadd_gw46   = _prompt_ip46(f"Node {_nadd_idx46} mgmt gateway")
                    except (EOFError, KeyboardInterrupt):
                        print("\n  ↩️  Node entry cancelled.")
                        break
                    _nadd_entry46 = {
                        "bmc": _nadd_bmc46,
                        "bmc_user": _nadd_user46,
                        "bmc_password": _nadd_pw46,
                        "node_mgmt_port": _nadd_port46,
                        "node_mgmt_ip": _nadd_ip46,
                        "node_mgmt_netmask": _nadd_mask46,
                        "node_mgmt_gateway": _nadd_gw46,
                    }
                    _config_data["secondary_nodes"].append(_nadd_entry46)
                    print(f"  \u2705 Node {_nadd_idx46} ({_nadd_bmc46}) added.")
                    _nadd_idx46 += 1
                print(f"\n  {_nadd_idx46 - 1} new node(s) added.")
                _session_log.log(f"4e build/add: {_nadd_idx46-1} secondary node(s) entered")
                _sp_ips46 = []  # SP IPs not queried in add path (no service-processor show needed)

        # ══════════════════════════════════════════════════════════════════
        # COMMON TAIL: optional "add extra nodes" (gather/create paths),
        # BMC_IP.json, write config
        # ══════════════════════════════════════════════════════════════════
        _skip_extra_nodes = (_mode46 == "build" and _build46 == "add")
        if not _skip_extra_nodes:
            # ── Prompt to add additional nodes ──────────────────────────────
            print("")
            print("  " + "─" * 58)
            _add_nodes46 = _prompt(
                "  Add additional nodes to the configuration? [y/N]: "
            , "n").lower()

            if _add_nodes46 == "y":
                print("")
                print("  Enter node details below.")
                print("  Leave BMC IP blank and press Enter when finished.\n")

                # Ensure secondary_nodes list exists in _config_data.
                if "secondary_nodes" not in _config_data:
                    _config_data["secondary_nodes"] = []

                _node_idx46 = 1
                while True:
                    _nbmc46 = _prompt(
                        f"  Node {_node_idx46} BMC IP (blank = done): "
                    )
                    if not _nbmc46:
                        break

                    def _prompt_req46(label):
                        """Prompt until a non-blank value is entered. Ctrl+C raises."""
                        while True:
                            val = input(f"  {label}: ").strip()
                            if val:
                                return val
                            print("    ⚠️  Value cannot be blank. Press Ctrl+C to cancel this node.")
                    try:
                        _nuser46 = input(
                            f"  Node {_node_idx46} BMC username [admin]: "
                        ).strip() or "admin"
                        _npass46 = getpass.getpass(
                            f"  Node {_node_idx46} BMC password (blank = none): "
                        )
                        _nport46 = input(
                            f"  Node {_node_idx46} management port [e0M]: "
                        ).strip() or "e0M"
                        _nip46   = _prompt_req46(f"Node {_node_idx46} management IP")
                        _nmask46 = _prompt_req46(f"Node {_node_idx46} management netmask")
                        _ngw46   = _prompt_req46(f"Node {_node_idx46} management gateway")
                    except (EOFError, KeyboardInterrupt):
                        print("\n  ↩️  Node entry cancelled.")
                        break

                    _nentry46 = {
                        "bmc": _nbmc46,
                        "bmc_user": _nuser46,
                        "bmc_password": _npass46,
                        "node_mgmt_port": _nport46,
                        "node_mgmt_ip": _nip46,
                        "node_mgmt_netmask": _nmask46,
                        "node_mgmt_gateway": _ngw46,
                    }

                    _config_data["secondary_nodes"].append(_nentry46)
                    print(f"  ✅ Node {_node_idx46} ({_nbmc46}) added.\n")
                    _node_idx46 += 1

                print(f"  {_node_idx46 - 1} node(s) added to configuration.")

        # ── Optionally create BMC_IP.json from SP addresses ─────────────
        print("")
        print("  " + "─" * 58)
        if _sp_ips46:
            print(f"  SP/BMC addresses gathered from 'service-processor show' ({len(_sp_ips46)}):")
            for _sip46 in _sp_ips46:
                print(f"    • {_sip46}")
            _bmc_ip_ans = _prompt(
                "  Write these to BMC_IP.json? [Y/n]: "
            , "y").lower()
        else:
            print("  ℹ️  No SP/BMC addresses were collected from the cluster shell.")
            _bmc_ip_ans = "n"

        if _bmc_ip_ans in ("", "y", "yes"):
            _bmc_ip_ips = _sp_ips46

            if _bmc_ip_ips:
                _bmc_ip_default = os.path.join(_snap_dir46, "BMC_IP.json")
                _bmc_ip_path = _prompt(
                    f"  Save to [{_bmc_ip_default}] (Enter to accept or type a new path): "
                )
                if _bmc_ip_path:
                    _bmc_ip_path = os.path.expanduser(os.path.expandvars(_bmc_ip_path))
                else:
                    _bmc_ip_path = _bmc_ip_default

                try:
                    os.makedirs(os.path.dirname(_bmc_ip_path), exist_ok=True)
                    with open(_bmc_ip_path, "w", encoding="utf-8") as _bmc_ip_f:
                        json.dump({"netboot_bmcs": _bmc_ip_ips}, _bmc_ip_f, indent=2)
                    print(f"\n  \u2705 BMC_IP.json written to: {_bmc_ip_path}")
                    _session_log.log(f"BMC_IP.json written: {_bmc_ip_path} ({_bmc_ip_ips})")
                except Exception as _bmc_ip_err:
                    print(f"  \u26a0\ufe0f  Could not write BMC_IP.json: {_bmc_ip_err}")
                    _session_log.log(f"BMC_IP.json write failed: {_bmc_ip_err}", prefix="WARN")
            else:
                print("  \u26a0\ufe0f  No BMC addresses collected; BMC_IP.json not written.")

        # Determine output path.
        # For build paths: always default to configs/add_nodes.json so the
        # source config file is never overwritten.  All other paths default to
        # configs/reinit-config.json.
        _snap_path46_default = (
            os.path.join(_snap_dir46, "add_nodes.json")
            if _mode46 == "build"
            else os.path.join(_snap_dir46, "reinit-config.json")
        )
        _custom46 = _prompt(
            f"  Save config to [{_snap_path46_default}] (Enter to accept or type a new path): "
        )
        _snap_path46 = (
            os.path.expanduser(os.path.expandvars(_custom46))
            if _custom46
            else _snap_path46_default
        )

        _written46 = write_config_snapshot(_snap_path46)
        if _written46:
            print(f"\n  \u2705 Backup configuration written to: {_written46}")
            _session_log.log(f"Config snapshot written to: {_written46}")
            _session_log.set_outcome("PASS", "backup config created")
        else:
            print("  \u26a0\ufe0f  Nothing was written (no cluster data captured).")
            _session_log.set_outcome("WARN", "no data captured; snapshot not written")

        _session_log.record_completion(normal_exit=True)
        print(f"\n\U0001f4dd Session log saved to: {_session_log.log_file}")
        sys.exit(0)

    # ── Mode 47 (4f): verify BMC authentication ────────────────────────────
    if _operation_mode == 47:
        _print_banner("\U0001f50d 4f: Verify BMC authentication")
        print("")

        # ── Locate BMC IP list ────────────────────────────────────────────
        # Search for BMC_IP.json or a full reinit-config that has node BMC IPs.
        _bmc_ips47 = []
        _found_file47 = None
        for _p47 in _find_config_files(
            candidate_names=("BMC_IP.json",
                             "reinit-config.json", "reinit_config.json",
                             "reinit-afx-config.json", "add_nodes.json"),
        ):
            try:
                with open(_p47, "r", encoding="utf-8") as _f47:
                    _d47data = json.load(_f47)
            except Exception:
                continue
            if isinstance(_d47data.get("netboot_bmcs"), list):
                _bmc_ips47 = [str(x) for x in _d47data["netboot_bmcs"] if x]
            else:
                _pn47 = _d47data.get("primary_node")
                if isinstance(_pn47, dict) and _pn47.get("bmc"):
                    _bmc_ips47.append(str(_pn47["bmc"]))
                for _sn47 in (_d47data.get("secondary_nodes") or []):
                    if isinstance(_sn47, dict) and _sn47.get("bmc"):
                        _bmc_ips47.append(str(_sn47["bmc"]))
                # Legacy nodes[]
                if not _bmc_ips47:
                    for _n47 in (_d47data.get("nodes") or []):
                        if isinstance(_n47, dict) and _n47.get("bmc"):
                            _bmc_ips47.append(str(_n47["bmc"]))
            if _bmc_ips47:
                _found_file47 = _p47
                break

        if _found_file47:
            print(f"  \U0001f4c4 Loaded {len(_bmc_ips47)} BMC address(es) from: {_found_file47}")
            for _ip47 in _bmc_ips47:
                print(f"     \u2022 {_ip47}")
        else:
            print("  \u2139\ufe0f  No BMC IP file found. Enter BMC addresses manually.")
            print("  (Leave blank and press Enter when done.)\n")
            _idx47 = 1
            while True:
                _entry47 = _prompt(f"  BMC {_idx47} hostname/IP (blank to finish): ")
                if not _entry47:
                    break
                _bmc_ips47.append(_entry47)
                _idx47 += 1

        if not _bmc_ips47:
            print("  No BMC addresses to test. Exiting.")
            sys.exit(0)

        # ── Credentials ──────────────────────────────────────────────────
        print("")
        _same_creds47 = input("  Use the same username and password for all BMCs? [Y/n]: ").strip().lower()
        _creds47 = {}   # ip -> (user, password)
        if _same_creds47 != "n":
            _shared_user47 = input("  BMC username [admin]: ").strip() or "admin"
            _shared_pass47 = getpass.getpass("  BMC password (blank = none): ")
            for _ip47 in _bmc_ips47:
                _creds47[_ip47] = (_shared_user47, _shared_pass47)
        else:
            for _ip47 in _bmc_ips47:
                print(f"\n  Credentials for {_ip47}:")
                _u47 = input("    Username [admin]: ").strip() or "admin"
                _p47 = getpass.getpass("    Password (blank = none): ")
                _creds47[_ip47] = (_u47, _p47)
        print("")

        # ── Test each BMC concurrently ────────────────────────────────────
        print("  " + "─" * 58)
        print(f"  Testing {len(_bmc_ips47)} BMC(s)…\n")

        _results47 = {}   # ip -> {"status": "PASS"|"FAIL", "detail": str}
        _results_lock47 = threading.Lock()

        def _test_bmc47(ip):
            detail = ""
            status = "FAIL"
            client47 = None
            ch47 = None
            _ip_user47, _ip_pass47 = _creds47.get(ip, ("admin", ""))
            try:
                client47 = paramiko.SSHClient()
                client47.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client47.connect(
                    hostname=ip, username=_ip_user47, password=_ip_pass47,
                    timeout=20, banner_timeout=30, auth_timeout=20,
                    disabled_algorithms={"pubkeys": ["ssh-dss"]},
                )
                ch47 = _open_shell(client47)

                # Wait for BMC prompt
                _buf47 = ""
                _t47 = time.monotonic()
                while time.monotonic() - _t47 < 15:
                    if ch47.recv_ready():
                        _buf47 += ch47.recv(4096).decode("utf-8", errors="replace")
                        if ">" in _buf47:
                            break
                    time.sleep(0.1)

                # Handle takeover prompt silently
                if "y/n" in _buf47.lower():
                    ch47.send("y\r")
                    _buf47 = ""
                    _t47 = time.monotonic()
                    while time.monotonic() - _t47 < 10:
                        if ch47.recv_ready():
                            _buf47 += ch47.recv(4096).decode("utf-8", errors="replace")
                            if ">" in _buf47:
                                break
                        time.sleep(0.1)

                # Run 'bmc status'
                ch47.send("bmc status\r")
                _out47 = ""
                _t47 = time.monotonic()
                while time.monotonic() - _t47 < 15:
                    if ch47.recv_ready():
                        chunk = ch47.recv(4096).decode("utf-8", errors="replace")
                        _out47 += chunk
                        if ">" in _out47[_out47.find("bmc status"):] if "bmc status" in _out47 else ">" in _out47:
                            break
                    time.sleep(0.1)

                # Parse IP from 'bmc status' output.
                # Look for lines like "  IP Address: 10.192.160.29"
                _found_ip47 = None
                for _ln47 in _out47.splitlines():
                    _m47 = re.search(
                        r'(?:ip\s*address|bmc\s*ip|address)\s*[:\s]+(\d{1,3}(?:\.\d{1,3}){3})',
                        _ln47, re.IGNORECASE,
                    )
                    if _m47:
                        _found_ip47 = _m47.group(1)
                        break

                if _found_ip47 is None:
                    # Fallback: any IPv4 in the output that isn't 0.0.0.0
                    for _m47 in re.finditer(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b', _out47):
                        _cand47 = _m47.group(1)
                        if _cand47 != "0.0.0.0":
                            _found_ip47 = _cand47
                            break

                if _found_ip47 and _found_ip47 == ip:
                    status = "PASS"
                    detail = f"bmc status IP matched ({_found_ip47})"
                elif _found_ip47:
                    status = "FAIL"
                    detail = f"IP mismatch: expected {ip}, got {_found_ip47}"
                else:
                    status = "FAIL"
                    detail = "could not parse IP from 'bmc status' output"

            except paramiko.AuthenticationException as _ex47a:
                status = "FAIL"
                detail = _classify_auth_failure(_ex47a)
            except Exception as _ex47:
                status = "FAIL"
                detail = _classify_auth_failure(_ex47)
            finally:
                try:
                    if ch47:
                        ch47.close()
                    if client47:
                        client47.close()
                except Exception:
                    pass

            with _results_lock47:
                _results47[ip] = {"status": status, "detail": detail}

        _run_parallel(_bmc_ips47, _test_bmc47, join_timeout=60)

        # ── Results table ─────────────────────────────────────────────────
        print("\n  " + "─" * 58)
        print(f"  {'BMC IP':<22}  {'Result':<6}  Detail")
        print(f"  {'─'*22}  {'─'*6}  {'─'*30}")
        _pass_count47 = 0
        _fail_count47 = 0
        for _ip47 in _bmc_ips47:
            _r47 = _results47.get(_ip47, {"status": "FAIL", "detail": "no result (timeout?)"})
            _icon47 = "\u2705" if _r47["status"] == "PASS" else "\u274c"
            print(f"  {_ip47:<22}  {_icon47} {_r47['status']:<4}  {_r47['detail']}")
            if _r47["status"] == "PASS":
                _pass_count47 += 1
            else:
                _fail_count47 += 1
        print(f"  {'─'*22}  {'─'*6}  {'─'*30}")
        print(f"\n  {_pass_count47} PASS  /  {_fail_count47} FAIL  (of {len(_bmc_ips47)} tested)\n")
        sys.exit(0)

    # ── Mode 45 (4d): set up passwordless SSH to cluster management ────────
    if _operation_mode == 45:
        import pathlib

        _print_banner("\U0001f511 Setting up passwordless SSH to cluster management")

        # Gather target details.
        mgmt_ip = input("\n  Cluster management IP address: ").strip()
        if not mgmt_ip:
            print("  No IP entered. Exiting.")
            sys.exit(0)
        ssh_user = input("  SSH username to configure: ").strip()
        if not ssh_user:
            print("  No username entered. Exiting.")
            sys.exit(0)

        # 1. Remove any existing known_hosts entries for this IP.
        known_hosts = pathlib.Path.home() / ".ssh" / "known_hosts"
        if known_hosts.exists():
            print(f"\n  \U0001f5d1\ufe0f  Removing existing known_hosts entries for {mgmt_ip}...")
            try:
                subprocess.run(
                    ["ssh-keygen", "-R", mgmt_ip],
                    check=False, capture_output=True,
                )
                print("  \u2705 Done.")
            except FileNotFoundError:
                print("  \u26a0\ufe0f  ssh-keygen not found on PATH; skipping known_hosts cleanup.")

        # 2. Generate RSA-4096 key if ~/.ssh/id_rsa doesn't already exist.
        id_rsa = pathlib.Path.home() / ".ssh" / "id_rsa"
        id_rsa_pub = pathlib.Path.home() / ".ssh" / "id_rsa.pub"
        if id_rsa.exists():
            print(f"\n  \u2139\ufe0f  Key pair already exists at {id_rsa}; skipping keygen.")
        else:
            print("\n  \U0001f511 Generating RSA-4096 key pair (no passphrase)...")
            (pathlib.Path.home() / ".ssh").mkdir(mode=0o700, parents=True, exist_ok=True)
            keygen_result = subprocess.run(
                ["ssh-keygen", "-t", "rsa", "-b", "4096",
                 "-f", str(id_rsa), "-N", ""],
                capture_output=True, text=True,
            )
            if keygen_result.returncode != 0:
                print(f"  \u274c ssh-keygen failed:\n{keygen_result.stderr}")
                sys.exit(1)
            print("  \u2705 Key pair generated.")

        # 3. Read the public key.
        if not id_rsa_pub.exists():
            print(f"  \u274c Public key not found at {id_rsa_pub}. Exiting.")
            sys.exit(1)
        pub_key = id_rsa_pub.read_text(encoding="utf-8").strip()
        print(f"\n  \U0001f4cb Public key:\n     {pub_key}")

        # 4. Connect via BMC and configure the public key on the cluster shell.
        print("\n  \U0001f4bb Connecting via BMC to configure cluster account...")

        # BMC credentials from config or interactive prompts.
        primary_node_45 = {}
        if isinstance(_config_data, dict):
            nl_45 = _config_data.get("nodes") or []
            if nl_45 and isinstance(nl_45[0], dict):
                primary_node_45 = nl_45[0]

        sp_host_45 = _cfg_str(primary_node_45.get("bmc"))
        if sp_host_45:
            _check_bmc_reachable(sp_host_45)
        else:
            sp_host_45 = _prompt_bmc_host()
        sp_user_45 = _cfg_str(primary_node_45.get("bmc_user")) or input("  BMC username: ").strip()
        if "bmc_password" in primary_node_45 and isinstance(primary_node_45["bmc_password"], str):
            sp_pass_45 = primary_node_45["bmc_password"]
        else:
            sp_pass_45 = getpass.getpass("  BMC password: ")

        _make_session_log("Mode 4d: set up passwordless SSH")

        _session_log.start_phase("SSH Connection (BMC)")
        client_45, sp_user_45, sp_pass_45 = connect_to_sp(sp_host_45, sp_user_45, sp_pass_45)
        ch_45 = _open_shell(client_45)
        threading.Thread(target=keepalive_loop, args=(client_45,), daemon=True).start()
        _session_log.end_phase()

        # Read the initial banner/prompt after shell open.  We watch for:
        #   ::>  / ::*>   – already at the cluster shell (console passthrough)
        #   y/n           – existing BMC session takeover prompt
        #   >             – plain BMC prompt (SP> or similar)
        # We do NOT call wait_for_bmc_prompt() because it consumes the ::>
        # and then the probe below can't see it.
        _session_log.start_phase("Cluster Shell Login")
        _init_out, _init_match = direct_read_until_any(
            ch_45,
            ["::>", "::*>", "y/n", ">"],
            timeout=20,
        )
        _already_at_cluster = False
        if _init_match and ("::>" in _init_match or "::*>" in _init_match):
            # Console is already in passthrough mode — no need for system console.
            _already_at_cluster = True
            print("  \u2705 Cluster shell prompt detected directly.")
        elif _init_match and "y/n" in _init_match.lower():
            # Existing session takeover prompt.
            print("\n  \u26a0\ufe0f  An existing BMC session is active.")
            _takeover = input("     Disconnect the other session? [Y/N]: ").strip().lower()
            if _takeover == "y":
                ch_45.send("y\r")
            else:
                print("  \u274c Cannot continue without taking over session. Exiting.")
                sys.exit(1)
            # Wait for BMC prompt after takeover.
            _to_out, _to_match = direct_read_until_any(
                ch_45, ["::>", "::*>", ">"], timeout=15
            )
            if _to_match and ("::>" in _to_match or "::*>" in _to_match):
                _already_at_cluster = True
        elif not _init_match:
            print("  \u274c No prompt received after BMC login. Exiting.")
            sys.exit(1)

        if not _already_at_cluster:
            enter_system_console(ch_45)

        if not _wait_for_cluster_prompt(ch_45, timeout=60):
            admin_pw_45 = _cluster_config.get("admin_password") or ""
            if not admin_pw_45:
                admin_pw_45 = getpass.getpass("  Cluster admin password: ")
            if not _login_primary_cluster_shell(ch_45, admin_pw_45):
                print("  \u274c Cluster shell login failed. Exiting.")
                sys.exit(1)
        _session_log.end_phase()

        # Get cluster name from the shell prompt itself — the ONTAP prompt
        # is "<clustername>::>" so this is more reliable than parsing command
        # output (which has column headers like "cluster" that look like data).
        ch_45.send("\r")
        _prompt_out, _ = direct_read_until_any(
            ch_45, ["::>", "::*>"], timeout=15
        )
        cluster_name_45 = ""
        for _pline in reversed((_prompt_out or "").splitlines()):
            _pm = re.match(r'^(\S+)::\*?>\s*$', _pline.strip())
            if _pm:
                cluster_name_45 = _pm.group(1)
                break
        if cluster_name_45:
            print(f"  \u2705 Cluster name detected from prompt: {cluster_name_45}")
        else:
            cluster_name_45 = input(
                "  Could not detect cluster name from prompt. Enter it manually: "
            ).strip()

        # Check whether the user has an SSH/publickey login entry.
        # Always check — even for admin — because the ssh application entry
        # may not exist even if the account does.
        print(f"\n  \U0001f50d Checking if '{ssh_user}' has an ssh/publickey login entry...")
        show_out = _run_cluster_command(
            ch_45,
            f"security login show {ssh_user} -application ssh "
            f"-authentication-method publickey",
            timeout=30,
        )
        ssh_login_exists = "no entries matching" not in show_out.lower()

        if not ssh_login_exists:
            print(f"  \U0001f194 No ssh login entry for '{ssh_user}'. Creating...")
            _run_cluster_command(
                ch_45,
                f"security login create -user-or-group-name {ssh_user} "
                f"-application ssh -authentication-method publickey "
                f"-role {'admin' if ssh_user.lower() == 'admin' else 'vsadmin'} "
                f"-vserver {cluster_name_45}",
                timeout=30,
            )
            print(f"  \u2705 SSH login entry created for '{ssh_user}'.")
        else:
            print(f"  \u2139\ufe0f  SSH login entry already exists for '{ssh_user}'.")
        print(f"\n  \U0001f511 Installing public key for '{ssh_user}' on cluster '{cluster_name_45}'...")
        _run_cluster_command(
            ch_45,
            f'security login publickey create -vserver {cluster_name_45} '
            f'-username {ssh_user} -publickey "{pub_key}"',
            timeout=30,
        )
        print("  \u2705 Public key installed on cluster.")

        # Close the BMC session — no longer needed.
        try:
            ch_45.close()
        except Exception:
            pass
        try:
            client_45.close()
        except Exception:
            pass

        # Test passwordless login from this host — open an interactive shell
        # and wait for the cluster prompt (::>) without a password prompt.
        print(f"\n  \U0001f50e Testing ssh {ssh_user}@{mgmt_ip}...")
        try:
            _pk_path_45 = os.path.expanduser("~/.ssh/id_rsa")
            _tc_45 = paramiko.SSHClient()
            _tc_45.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            _tc_45.connect(
                mgmt_ip, username=ssh_user,
                key_filename=_pk_path_45,
                look_for_keys=True, allow_agent=False,
                timeout=20,
            )
            _tch_45 = _tc_45.invoke_shell(width=200, height=50)
            _tout_45, _tmatch_45 = direct_read_until_any(
                _tch_45, ["::>", r"::\*>", "password:", "Password:"], timeout=30
            )
            try:
                _tch_45.close()
            except Exception:
                pass
            _tc_45.close()
            if "::" in _tmatch_45:
                print("  \u2705 Passwordless login configuration complete!")
                _slog(f"Passwordless SSH verified: {ssh_user}@{mgmt_ip}")
            else:
                print(
                    f"  \u26a0\ufe0f  Cluster prompted for a password — "
                    f"key may need a moment to activate.\n"
                    f"     Test manually with: ssh {ssh_user}@{mgmt_ip}"
                )
                _slog("SSH test: password prompt appeared", prefix="WARN")
        except paramiko.AuthenticationException:
            print(
                f"  \u26a0\ufe0f  Authentication failed — public key not accepted yet.\n"
                f"     Test manually with: ssh {ssh_user}@{mgmt_ip}"
            )
            _slog("SSH test: AuthenticationException", prefix="WARN")
        except Exception as _te_45:
            print(
                f"  \u26a0\ufe0f  SSH test failed: {_te_45}\n"
                f"     Test manually with: ssh {ssh_user}@{mgmt_ip}"
            )
            _slog(f"SSH test exception: {_te_45}", prefix="WARN")

        _session_log.record_completion(normal_exit=True)
        print(f"\n\U0001f4dd Session log saved to: {_session_log.log_file}")
        sys.exit(0)

    # Optional configuration file. CLI flag takes precedence; otherwise we
    # offer to load one interactively. Type '?' at the prompt to view the
    # expected JSON schema.
    config_path = args.config

    # Auto-detect a default config file via the shared discovery helper
    # (searches configs/, script dir, then CWD with the canonical filename
    # list and a deep-scan fallback for oddly-named JSONs containing the
    # cluster+nodes schema).
    search_dirs = _default_config_search_dirs()
    detected_configs = _find_config_files(
        search_dirs=search_dirs, deep_scan=True
    )

    if config_path:
        # CLI-supplied path: validate before continuing.
        if not os.path.isfile(config_path):
            print(f"❌ --config path is not a file: {config_path}")
            sys.exit(1)
    else:
        # Offer the auto-detected default(s) first.
        # Mode 2 (add nodes) also participates: a config file supplies node
        # management IPs, netmask, gateway and the cluster management IP so
        # the join wizard can be fully automated without manual prompts.
        if detected_configs:
            config_path = _select_config_path_interactive(
                detected_configs, indent="", header_emoji="📄"
            )
        else:
            # Nothing auto-detected — tell the user where we looked so they
            # can spot a wrong directory or filename quickly.
            print("\nℹ️  No config file auto-detected. Searched:")
            for d in search_dirs:
                print(f"     {d}")
            print(f"     (looking for: {', '.join(_DEFAULT_CONFIG_FILENAMES)})")

        if not config_path:
            while True:
                ans = _prompt(
                    "\nUse a JSON config file for inputs? "
                    "Enter path, '?' for example, or blank to skip: "
                )
                if ans == "?":
                    print(_CONFIG_FILE_EXAMPLE)
                    continue
                if not ans:
                    break
                # Strip surrounding quotes that shells/users often paste in.
                if (len(ans) >= 2 and ans[0] == ans[-1]
                        and ans[0] in ("'", '"')):
                    ans = ans[1:-1]
                expanded = os.path.expanduser(os.path.expandvars(ans))
                if not os.path.isfile(expanded):
                    print(f"  ⚠️  Not a valid file path: {ans}")
                    print("     Enter an existing file, '?' for example, "
                          "or blank to skip.")
                    continue
                config_path = expanded
                break

    # ---- No config file? Offer the "reuse existing cluster configuration"
    # path up-front. The retain capture itself still runs later (after the
    # BMC connection is up), but asking the question here lets the script
    # skip a duplicate prompt mid-run AND, when the operator answers yes,
    # the retained values flow straight into the in-memory config so the
    # rest of the pipeline behaves as if the JSON had supplied them.
    # Retain-from-existing-cluster only makes sense when we're going to
    # initialize the cluster (modes 1 and 3); mode 2/4/5 skip it.

    if config_path:
        try:
            _config_data = load_config_file(config_path)
            print(f"📄 Loaded config: {config_path}")
            # Config file supplies all cluster values — no need to pull them
            # from a running cluster later. Mark retain as "no" so the
            # mid-run retain prompt is suppressed.
            _run_context.config_data = _config_data
            _run_context.retain_preselected = (False, False, False)
            _run_context.apply_to_globals()
        except ValueError as e:
            print(f"⚠️  {e}")
            print("   Continuing without a config file (manual prompts).")
            _config_data = {}
            _run_context.config_data = _config_data
            _run_context.apply_to_globals()

    if not config_path and _operation_mode in (1, 3):
        _print_banner("💾 No config file in use — reuse existing cluster config?")
        print("\n  If this BMC's node is part of a running cluster, the script")
        print("  can pull the existing cluster name and management/network IPs")
        print("  from it and use them as the new configuration so you don't")
        print("  have to re-enter them.")
        ans1 = _prompt(
            "\n  Reuse the existing cluster name? [Y/N]: "
        , "n").lower()
        retain_name = (ans1 == "y")

        ans2 = _prompt(
            "  Reuse the existing management and cluster network IPs? "
            "[Y/N]: "
        , "n").lower()
        retain_network = (ans2 == "y")

        ans3 = _prompt(
            "  Reuse the BMC admin user and password as the cluster "
            "admin user and password? [Y/N]: "
        , "n").lower()
        retain_creds = (ans3 == "y")

        _run_context.retain_preselected = (retain_name, retain_network, retain_creds)
        _run_context.apply_to_globals()
        if retain_name or retain_network or retain_creds:
            print("\n  ↩️  Will pull the requested values from the running cluster")
            print("     (and/or reuse BMC credentials) after connecting to the BMC,")
            print("     then build the runtime config from them.")
        else:
            print("\n  ↩️  Will not retain any existing cluster configuration.")

    # License: collect key(s) or validate the license file path now, before
    # the BMC session starts, so the operator can fix issues early.
    if _operation_mode in (1, 3):
        _collect_license_config(_run_context)

    _make_session_log("Session start")

    if _config_data:
        _session_log.log(f"Loaded config file: {config_path}")

    if _operation_mode == 3:
        mode_desc = "End-to-end auto initialize (1b primary + parallel auto-add peers)"
    elif _operation_mode == 1 and _auto_setup:
        mode_desc = "Initialize + format + setup first node (1b, fully automated)"
    elif _operation_mode == 1:
        mode_desc = "Initialize first node (1a, option 9, destroy storage pods)"
    elif _operation_mode == 41:
        mode_desc = "ONTAP upgrade - rolling takeover/giveback (4a)"
    elif _operation_mode == 44:
        mode_desc = "Install license file only (4c, standalone)"
    elif _operation_mode == 45:
        mode_desc = "Set up passwordless SSH to cluster management (4d)"
    elif _operation_mode == 46:
        mode_desc = "Create backup cluster configuration (4e, standalone)"
    elif _operation_mode == 2 and _auto_add:
        mode_desc = "Add node to existing cluster (2b, automatic join wizard)"
    else:
        mode_desc = "Add node to existing cluster (2a, option 4, interactive)"
    _session_log.log(
        f"Operation mode: {_operation_mode} (auto_setup={_auto_setup}, "
        f"auto_add={_auto_add}) – {mode_desc}"
    )

    # Primary BMC: the node this script will connect to and operate on.
    # For modes 1/3/4x: prefer "primary_node" (new format) or nodes[0] (legacy).
    # For mode 2 (add node): the target is a *secondary* node — use nodes[]
    #   (new or legacy), NOT primary_node (which is the existing cluster node
    #   that must never be reinitialised).
    primary_node = {}
    if isinstance(_config_data, dict):
        if _operation_mode == 2:
            # Mode 2: pick first entry from secondary_nodes or nodes[] only.
            _sn2 = _config_data.get("secondary_nodes")
            if isinstance(_sn2, list) and _sn2 and isinstance(_sn2[0], dict):
                primary_node = _sn2[0]
            else:
                _nl2 = _config_data.get("nodes") or []
                if _nl2 and isinstance(_nl2[0], dict):
                    primary_node = _nl2[0]
        elif isinstance(_config_data.get("primary_node"), dict):
            primary_node = _config_data["primary_node"]
        else:
            nodes_list = _config_data.get("nodes") or []
            if nodes_list and isinstance(nodes_list[0], dict):
                primary_node = nodes_list[0]

    # Three-state handling for each config field:
    #   * key absent / non-string  -> prompt the operator
    #   * key present but empty/whitespace -> use as-is (e.g. "" means
    #     literally no password, for BMCs that don't require one)
    #   * non-empty string         -> use the provided value
    # (_cfg_str is defined at module level; _cfg_get_or_prompt is local
    # because it closes over `primary_node`.)
    def _cfg_get_or_prompt(key, prompt_label, hidden=False):
        if key in primary_node and isinstance(primary_node[key], str):
            return primary_node[key]
        if hidden:
            return getpass.getpass(prompt_label)
        return input(prompt_label)

    sp_host = _cfg_str(primary_node.get("bmc"))
    if sp_host:
        _check_bmc_reachable(sp_host)
    else:
        sp_host = _prompt_bmc_host("Enter SP hostname/IP: ")
    sp_user = _cfg_str(primary_node.get("bmc_user")) or input("Enter SP username: ")
    sp_pass = _cfg_get_or_prompt("bmc_password", "Enter SP password: ", hidden=True)
    if primary_node.get("bmc"):
        _pn_src = "primary_node" if isinstance(_config_data.get("primary_node"), dict) else "nodes[0]"
        print(f"📄 Using primary BMC from config {_pn_src}: {sp_host} (user={sp_user})")
        if "bmc_password" in primary_node and not (
                isinstance(primary_node["bmc_password"], str)
                and primary_node["bmc_password"].strip()):
            print("📄 Primary BMC password from config is blank "
                  "(will attempt SSH with no password).")

    _session_log.log(f"Target BMC: {sp_host}")
    _session_log.log(f"Username: {sp_user}")
    _session_log.log(f"Debug mode: {args.debug}")

    # Phase: SSH Connection
    _session_log.start_phase("SSH Connection")
    client, sp_user, sp_pass = connect_to_sp(sp_host, sp_user, sp_pass)
    channel = _open_shell(client)

    keepalive_thread = threading.Thread(
        target=keepalive_loop, args=(client,), daemon=True
    )
    keepalive_thread.start()
    _session_log.log("Keepalive thread started")
    _session_log.end_phase()

    # Phase: BMC Prompt & Validation
    _session_log.start_phase("BMC Prompt & Validation")
    if not wait_for_bmc_prompt(channel, auto_takeover=True):
        _session_log.set_outcome("FAIL", "BMC prompt not received")
        _session_log.close()
        sys.exit(1)

    print("\nValidating BMC status...")
    _session_log.log("Validating BMC status")
    drain_channel(channel, seconds=1)
    output = direct_send_and_wait(channel, "bmc status", ">", timeout=15)
    if sp_host in output:
        print(f"\n✅ BMC validation successful – found '{sp_host}' in status output.")
        _session_log.log(f"BMC validation successful – found '{sp_host}'")
    else:
        print(f"\n⚠️  Warning: '{sp_host}' not found verbatim in bmc status output.")
        print(f"   Output received:\n{output}")
        _session_log.log(f"BMC validation warning – '{sp_host}' not found", prefix="WARN")
        answer = input("\n   Does this look like the correct BMC? [Y/N]: ").strip().lower()
        _session_log.log_user_input(f"BMC validation confirmation: {answer}")
        if answer != "y":
            print("❌ BMC validation rejected. Exiting.")
            _session_log.log("BMC validation rejected", prefix="ERROR")
            _session_log.set_outcome("FAIL", "BMC validation rejected by operator")
            _session_log.close()
            sys.exit(1)
        print("✅ BMC validation confirmed by user.")
        _session_log.log("BMC validation confirmed by user")
    _session_log.end_phase()

    # Mode 1 / 3: ask retain prompts, then ALWAYS attempt peer-BMC discovery
    # (even if the user answered 'n' to both retain prompts, and regardless
    # of whether retain captures succeed or fail). Discovery may itself fail
    # if the node is already down – in that case we proceed with no peer
    # reset / peer add.
    if _operation_mode in (1, 3):
        if _retain_preselected is not None:
            retain_name, retain_network, retain_creds = _retain_preselected
            # When a config file was loaded all three are False — just note
            # that retain is not needed and move on without extra output.
            if not (retain_name or retain_network or retain_creds):
                _session_log.log(
                    "Retain skipped: config file in use"
                )
            else:
                # Operator answered up-front (no config file path used).
                print("\n" + "=" * 60)
                print("  \U0001f4be Retain Existing Cluster Configuration "
                      "(pre-answered above)")
                print("=" * 60)
                print(f"  Retain cluster name : {'yes' if retain_name else 'no'}")
                print(f"  Retain network IPs  : {'yes' if retain_network else 'no'}")
                print(f"  Reuse BMC creds     : {'yes' if retain_creds else 'no'}")
                _session_log.log(
                    f"Retain choices pre-selected (no config file): "
                    f"name={retain_name}, network={retain_network}, "
                    f"creds={retain_creds}"
                )
        else:
            _print_banner("💾 Retain Existing Cluster Configuration?")
            ans1 = input("\n  Do you want to retain the cluster name? [Y/N]: ").strip().lower()
            _session_log.log_user_input(f"Retain cluster name? {ans1}")
            retain_name = (ans1 == "y")

            ans2 = input(
                "  Do you want to retain the same management and cluster network IPs\n"
                "  from your existing cluster? [Y/N]: "
            ).strip().lower()
            _session_log.log_user_input(f"Retain network IPs? {ans2}")
            retain_network = (ans2 == "y")

            ans3 = input(
                "  Reuse the BMC admin user and password as the cluster\n"
                "  admin user and password? [Y/N]: "
            ).strip().lower()
            _session_log.log_user_input(f"Reuse BMC creds as cluster admin? {ans3}")
            retain_creds = (ans3 == "y")

        # If the operator opted to reuse BMC creds for the cluster admin,
        # promote them into the in-memory cluster config so the rest of
        # the script (cluster setup wizard, peer-add, etc.) treats them
        # as "from config" with no further prompting.
        if retain_creds:
            cc_block = _config_data.setdefault("cluster", {}) if isinstance(_config_data, dict) else None
            if isinstance(cc_block, dict):
                if not cc_block.get("user") and _primary_bmc_user:
                    cc_block["user"] = _primary_bmc_user
                # Always overwrite any blank/missing password; if the
                # operator explicitly provided a different one in the
                # config we leave it alone.
                if not cc_block.get("password") and _primary_bmc_password is not None:
                    cc_block["password"] = _primary_bmc_password
                _session_log.log(
                    "Reused BMC admin user/password as cluster admin "
                    "credentials in runtime config"
                )
                print("\n  🔐 Cluster admin user/password will reuse the BMC "
                      "login credentials.")

        if not (retain_name or retain_network):
            print("\n  ↩️  Skipping retain capture; will still discover peer BMC")
            print("     addresses so peer nodes can be reset to LOADER.")
            _session_log.log("User declined retain; proceeding with peer SP discovery only")

        # If peer BMC addresses are already present in the config file we can
        # skip the cluster-shell login entirely (no need to probe the console).
        # Also skip entirely when a config file was loaded (_retain_preselected
        # is the (False, False, False) sentinel) — the config is the source of
        # truth; no cluster-shell probing is needed.
        _cfg_has_peers = False
        if isinstance(_config_data, dict):
            _sn_check = _config_data.get("secondary_nodes")
            if isinstance(_sn_check, list) and any(
                isinstance(n, dict) and n.get("bmc") for n in _sn_check
            ):
                _cfg_has_peers = True
            else:
                _nodes_check = _config_data.get("nodes") or []
                if len([n for n in _nodes_check if isinstance(n, dict) and n.get("bmc")]) > 1:
                    _cfg_has_peers = True

        _config_file_loaded = (_retain_preselected == (False, False, False))

        if not (retain_name or retain_network) and (_cfg_has_peers or _config_file_loaded):
            if _config_file_loaded:
                print("\n  📄 Config file in use — skipping cluster-shell discovery.")
            else:
                print("\n  📄 Peer BMC addresses already available in config file.")
                print("     Skipping cluster-shell discovery.")
            _session_log.log("Skipping collect_retain_data: config file supplied peer BMCs")
            peer_addresses = []
        else:
            # Peer SP discovery is independent of the retain answers and runs in
            # the same console session for efficiency. If the cluster shell can't
            # be reached, all captures (retain + peer SPs) are skipped gracefully.
            _, _, peer_addresses = collect_retain_data(
                channel, retain_name, retain_network, collect_peer_sps=True
            )

        # Promote any retained cluster details (name, cluster-mgmt LIF,
        # default gateway) into the in-memory JSON config so the cluster
        # setup wizard treats them as "from config" without re-prompting.
        # Any field already supplied by the operator's config file is left
        # alone. When the retain capture failed, this is a no-op and the
        # operator falls through to manual prompts later.
        apply_retained_to_cluster_config()

        # Likewise, promote per-node management LIFs (port, IP, netmask,
        # gateway) into the corresponding _config_data["nodes"] entries
        # using the SP-address -> node-name mapping captured alongside.
        # This lets the per-BMC node-mgmt collector use retained values
        # as defaults / silent values, including for peer BMCs that the
        # operator didn't pre-list in the JSON config.
        apply_retained_to_node_configs(primary_bmc=sp_host)

        # Build the unique peer-BMC list. Sources, in priority order:
        #   1. SP addresses discovered from the running cluster.
        #   2. `nodes[]` entries from the JSON config file.
        # Any entry matching the primary BMC (`sp_host`) is dropped so the
        # script never tries to "add" the node it just initialized, and
        # duplicates are removed while preserving order.
        seen_peers = {sp_host}
        other_sps = []

        for a in (peer_addresses or []):
            if not a:
                continue
            if a in seen_peers:
                if a == sp_host:
                    _session_log.log(
                        f"Discovered peer entry matches primary BMC ({a}); "
                        "treating as primary only.",
                    )
                continue
            seen_peers.add(a)
            other_sps.append(a)

        cfg_nodes = []
        if isinstance(_config_data, dict):
            # New format: secondary_nodes only (primary is never a peer).
            _sn = _config_data.get("secondary_nodes")
            if isinstance(_sn, list):
                cfg_nodes = [n for n in _sn if isinstance(n, dict)]
            else:
                # Legacy format: walk ALL nodes[] entries. The primary is
                # filtered out below via the `bmc == sp_host` check, so
                # nodes[0] is included here — it may be a peer when the
                # actual primary BMC was entered manually and doesn't match
                # any config entry.
                _all_nodes = _config_data.get("nodes") or []
                cfg_nodes = [n for n in _all_nodes if isinstance(n, dict)]
        cfg_peer_added = []
        for node in cfg_nodes:
            if not isinstance(node, dict):
                continue
            bmc = (node.get("bmc") or "").strip()
            if not bmc:
                continue
            if bmc == sp_host:
                _session_log.log(
                    f"Config 'nodes[]' entry {bmc} matches primary BMC; "
                    "treating as primary only.",
                )
                continue
            if bmc in seen_peers:
                continue
            seen_peers.add(bmc)
            other_sps.append(bmc)
            cfg_peer_added.append(bmc)

        if cfg_peer_added:
            print(f"\n  📄 Added {len(cfg_peer_added)} peer BMC(s) from config: "
                  f"{', '.join(cfg_peer_added)}")
            _session_log.log(f"Peers added from config nodes[]: {cfg_peer_added}")

        # If neither discovery nor the config file yielded peer BMCs (single
        # node, node already down, or capture failed), look for JSON files in
        # the configs/ directory before falling back to manual entry.
        if not other_sps:
            print("\n  ℹ️  No peer service-processor addresses discovered")
            print("     (single-node cluster, node already down, or capture failed).")
            _session_log.log("No peer SP addresses auto-discovered; checking configs dir")

            # Search configs/ and script dir for JSON files with BMC entries.
            import json as _json_pb
            _pb_candidates = []
            try:
                _pb_script_dir = os.path.dirname(os.path.abspath(__file__))
            except NameError:
                _pb_script_dir = os.getcwd()
            _pb_configs_dir = os.path.join(_pb_script_dir, "configs")
            for _pb_dir in [_pb_configs_dir, _pb_script_dir]:
                if not os.path.isdir(_pb_dir):
                    continue
                for _pb_fname in sorted(os.listdir(_pb_dir)):
                    if not _pb_fname.lower().endswith(".json"):
                        continue
                    _pb_fpath = os.path.abspath(os.path.join(_pb_dir, _pb_fname))
                    try:
                        with open(_pb_fpath, "r", encoding="utf-8") as _pbf:
                            _pb_data = _json_pb.load(_pbf)
                        _pb_ips = []
                        _pbn = _pb_data.get("primary_node")
                        _pbsn = _pb_data.get("secondary_nodes")
                        _pbnodes = _pb_data.get("nodes")
                        if isinstance(_pbn, dict) and _pbn.get("bmc"):
                            _pb_ips.append(str(_pbn["bmc"]))
                        for _n in (_pbsn or []):
                            if isinstance(_n, dict) and _n.get("bmc"):
                                _pb_ips.append(str(_n["bmc"]))
                        if not _pb_ips and isinstance(_pbnodes, list):
                            _pb_ips = [str(n["bmc"]) for n in _pbnodes
                                       if isinstance(n, dict) and n.get("bmc")]
                        if isinstance(_pb_data.get("netboot_bmcs"), list):
                            _pb_ips = [str(x) for x in _pb_data["netboot_bmcs"] if x]
                        # Only include files that have peer BMCs beyond sp_host.
                        _pb_peers = [ip for ip in _pb_ips
                                     if ip and ip not in seen_peers and ip != sp_host]
                        if _pb_peers:
                            _pb_candidates.append((_pb_fpath, _pb_data, _pb_peers))
                    except Exception:
                        pass

            if _pb_candidates:
                print(f"\n  Found {len(_pb_candidates)} config file(s) with peer BMC addresses:")
                for _pbi, (_pb_fpath, _, _pb_peers) in enumerate(_pb_candidates, 1):
                    print(f"    {_pbi}. {_pb_fpath}  "
                          f"({len(_pb_peers)} peer(s): {', '.join(_pb_peers)})")
                print("    0. Enter peer BMC addresses manually")
                print("")
                while True:
                    _pb_sel = _prompt("  Load peers from a file? [1] or 0 for manual: ", "0")
                    if _pb_sel == "" and len(_pb_candidates) == 1:
                        _pb_sel = "1"
                    if _pb_sel == "0":
                        break
                    if _pb_sel.isdigit() and 1 <= int(_pb_sel) <= len(_pb_candidates):
                        _, _, _pb_peers = _pb_candidates[int(_pb_sel) - 1]
                        for _pb_ip in _pb_peers:
                            seen_peers.add(_pb_ip)
                            other_sps.append(_pb_ip)
                        print(f"\n  ✅ Loaded {len(_pb_peers)} peer BMC(s): "
                              f"{', '.join(_pb_peers)}")
                        _session_log.log(f"Peer BMCs loaded from file: {_pb_peers}")
                        break
                    print("  ⚠️  Invalid selection.")

            if not other_sps:
                # No files found or operator chose manual entry.
                print("\n  You may enter peer BMC IPs/hostnames manually so this script")
                print("  can reset them to LOADER. Enter one per prompt; press Enter on")
                print("  an empty line to finish and continue.")

                i = 1
                while True:
                    entry = input(f"\n  Peer BMC #{i} (blank to finish): ").strip()
                    _session_log.log_user_input(f"Manual peer BMC #{i}: {entry!r}")
                    if not entry:
                        break
                    if entry in seen_peers:
                        print(f"  ⚠️  '{entry}' already added or is the primary BMC; skipping.")
                        _session_log.log(f"Duplicate/primary peer BMC entry skipped: {entry}")
                        continue
                    seen_peers.add(entry)
                    other_sps.append(entry)
                    print(f"  ✅ Added peer BMC: {entry}")
                    _session_log.log(f"Manually added peer BMC: {entry}")
                    i += 1

            if other_sps:
                print(f"\n  ✅ Will reset {len(other_sps)} peer BMC(s) to LOADER: "
                      f"{', '.join(other_sps)}")
                _session_log.log(f"Peer BMCs to reset: {other_sps}")
            else:
                print("\n  ↩️  No peer BMCs entered; proceeding with reset of this node only.")
                _session_log.log("User entered no peer BMCs manually")

        # ---- Confirm the discovered peer count, with the option to add
        # more node entries on the fly. New entries are written back into
        # the in-memory _config_data["nodes"] list so the rest of the
        # pipeline (per-node mgmt collection, peer credential lookup, etc.)
        # treats them identically to anything that was in the JSON to
        # begin with.
        def _prompt_with_default(label, default=None):
            suffix = f" [{default}]" if default else ""
            val = _prompt(f"    {label}{suffix}: ")
            return val or (default or "")

        while True:
            _print_banner("📋 Cluster node summary")
            print(f"  BMC of first node in the cluster : {sp_host} (user={sp_user})")
            if other_sps:
                print(f"  Nodes to add after cluster init  ({len(other_sps)}):")
                for a in other_sps:
                    print(f"    - {a}")
            else:
                print("  Nodes to add after cluster init  : (none)")
            print(f"  Total nodes                      : {1 + len(other_sps)}")
            ans = _prompt("\n  Is this the correct number of nodes? [Y/N]: ", "y").lower()
            _session_log.log_user_input(f"Confirm node count ({1 + len(other_sps)}): {ans}")
            if ans in ("", "y", "yes"):
                global _initial_node_count
                _initial_node_count = 1 + len(other_sps)
                _session_log.log(f"Initial node count confirmed: {_initial_node_count}")
                break

            print("\n  ➕ Add one or more additional peer node(s). Enter the same")
            print("     fields used by the JSON config file. Blank BMC ends entry.")
            added_this_round = 0
            while True:
                new_bmc = _prompt("\n    New peer BMC IP/hostname (blank to finish): ")
                if not new_bmc:
                    break
                if new_bmc == sp_host or new_bmc in seen_peers:
                    print(f"    ⚠️  '{new_bmc}' is already the primary or a known peer; skipping.")
                    _session_log.log(f"Duplicate add-peer entry skipped: {new_bmc}")
                    continue
                new_user = _prompt_with_default(f"BMC username for {new_bmc}", sp_user)
                try:
                    new_pass = getpass.getpass(
                        f"    BMC password for {new_user}@{new_bmc} "
                        "(blank = no password): "
                    )
                except (EOFError, KeyboardInterrupt):
                    new_pass = ""
                new_port = _prompt_with_default(
                    f"Node mgmt port for {new_bmc}", "e0M")
                new_ip = _prompt_with_default(f"Node mgmt IP for {new_bmc}")
                new_mask = _prompt_with_default(
                    f"Node mgmt netmask for {new_bmc}", "255.255.255.0")
                new_gw = _prompt_with_default(f"Node mgmt gateway for {new_bmc}")

                new_entry = {
                    "bmc": new_bmc,
                    "bmc_user": new_user,
                    "bmc_password": new_pass,
                    "node_mgmt_port": new_port,
                    "node_mgmt_ip": new_ip,
                    "node_mgmt_netmask": new_mask,
                    "node_mgmt_gateway": new_gw,
                }

                # Persist into the in-memory config so _node_cfg_for() picks
                # it up later. We never write back to disk; the JSON file
                # itself is left untouched.
                if isinstance(_config_data.get("secondary_nodes"), list):
                    _config_data["secondary_nodes"].append(new_entry)
                elif "primary_node" in _config_data:
                    _config_data.setdefault("secondary_nodes", []).append(new_entry)
                else:
                    _config_data.setdefault("nodes", []).append(new_entry)

                seen_peers.add(new_bmc)
                other_sps.append(new_bmc)
                added_this_round += 1
                print(f"    ✅ Added peer BMC: {new_bmc}")
                _session_log.log(
                    f"Operator added peer BMC at confirmation step: {new_bmc} "
                    f"(user={new_user}, port={new_port}, ip={new_ip})"
                )

            if added_this_round == 0:
                print("\n  (No new peers entered; re-confirming current list.)")

        # Collect node-management network info for every BMC (primary +
        # peers) up-front so mode 1b can auto-answer the per-node prompts.
        # This also runs in mode 1a so the data is captured in the log even
        # though it won't be auto-applied.
        collect_node_mgmt_per_bmc(sp_host, other_sps)

        # Mode 1b also needs the cluster-level setup wizard answers up-front.
        if _auto_setup:
            collect_cluster_config()

        if other_sps:
            _print_banner("🔐 Peer BMC SSH Credentials")
            print("\n  Provide SSH credentials for each peer BMC. Press Enter")
            print(f"  to reuse the primary BMC username '{sp_user}' / password.")
            for addr in other_sps:
                print(f"\n  ── Peer BMC {addr} ──")
                node_cfg = _node_cfg_for(addr)
                # Three-state handling for each field:
                #   * Key absent / non-string  -> prompt the operator.
                #   * Key present (even empty) -> use the value as-is. An
                #     empty string means literally "no password" for BMCs
                #     that don't require one (or accept passthrough creds).
                #   * Non-empty string         -> use as-is.
                user_in_cfg = (
                    "bmc_user" in node_cfg
                    and isinstance(node_cfg["bmc_user"], str)
                )
                pass_in_cfg = (
                    "bmc_password" in node_cfg
                    and isinstance(node_cfg["bmc_password"], str)
                )
                # Resolve username.
                if user_in_cfg:
                    u = node_cfg["bmc_user"].strip() or sp_user
                    if not node_cfg["bmc_user"].strip():
                        print(f"    📄 Username blank in config for {addr}; "
                              f"reusing primary user '{sp_user}'.")
                else:
                    try:
                        u = input(
                            f"    Username for {addr} "
                            f"[hit enter to re-use {sp_user}]: "
                        ).strip() or sp_user
                    except (EOFError, KeyboardInterrupt):
                        u = sp_user

                # Resolve password.
                if pass_in_cfg:
                    p = node_cfg["bmc_password"]
                    if p:
                        print(f"    📄 Using config credentials for {addr} (user={u})")
                    else:
                        print(f"    📄 Password blank in config for {addr}; "
                              "will attempt SSH with no password.")
                else:
                    p = getpass.getpass(
                        f"    Password for {addr} (blank to reuse primary): "
                    )
                    if not p:
                        p = sp_pass
                _peer_bmc_creds[addr] = {"user": u, "password": p}
                if p == sp_pass:
                    pw_desc = "<reused-primary>"
                elif p == "":
                    pw_desc = "<blank>"
                else:
                    pw_desc = "<custom>"
                _session_log.log(
                    f"Captured credentials for peer BMC {addr} (user={u}, "
                    f"password={pw_desc})"
                )

        if _operation_mode == 3:
            # Mode 3: peers will be auto-added in parallel AFTER the primary's
            # cluster create completes. Stash the peer list for the wizard
            # post-step.
            _run_context.peer_bmc_list = list(other_sps)
            _run_context.apply_to_globals()
            if other_sps:
                print(f"\n  🧩 Mode 3: {len(other_sps)} peer node(s) will be"
                      " auto-added in parallel after primary cluster is up:")
                print(f"     {', '.join(other_sps)}")
                _session_log.log(f"Mode 3 peer add list: {other_sps}")

        # Reset every peer BMC to LOADER up-front (modes 1 and 3 both need
        # peers parked at LOADER before the primary runs option 9 / before
        # the mode 3 parallel auto-add kicks in).
        if other_sps:
            _print_banner(f"🔁 Resetting {len(other_sps)} peer node(s) to LOADER (parallel)")
            print(f"  Peer BMCs: {', '.join(other_sps)}")
            _session_log.start_phase("Peer Node Reset to LOADER")
            _session_log.log(f"Peer BMCs to reset: {other_sps}")

            # Open a dedicated log file per peer so parallel console streams
            # don't interleave on the terminal.
            _pr_log_dir = _session_log.log_dir if _session_log else os.getcwd()
            _pr_node_logs: dict = {}
            for addr in other_sps:
                try:
                    _pr_nf = _node_log_open(addr, _pr_log_dir, prefix="peer_reset")
                    _pr_node_logs[addr] = _pr_nf
                    print(f"  📝 [{addr}] Reset log → {_pr_nf.name}")
                    _session_log.log(f"[{addr}] reset log: {_pr_nf.name}")
                except Exception as _pr_e:
                    _pr_node_logs[addr] = None
                    print(f"  ⚠️  [{addr}] Could not open reset log: {_pr_e}")

            _pr_results: dict = {}
            _pr_lock = threading.Lock()

            def _pr_worker(addr):
                creds = _peer_bmc_creds.get(addr, {"user": sp_user, "password": sp_pass})
                ok = reset_peer_to_loader(
                    addr, creds["user"], creds["password"],
                    node_log=_pr_node_logs.get(addr),
                )
                with _pr_lock:
                    _pr_results[addr] = ok

            _run_parallel(other_sps, _pr_worker)

            for addr, nf in _pr_node_logs.items():
                if nf:
                    try:
                        nf.close()
                    except Exception:
                        pass

            print("")
            for addr in other_sps:
                ok = _pr_results.get(addr, False)
                sym = "✅" if ok else "⚠️ "
                _session_log.log(
                    f"[{addr}] peer reset {'reached LOADER' if ok else 'did NOT reach LOADER'}"
                )
                print(f"  {sym} [{addr}] Peer reset {'reached LOADER' if ok else 'did NOT reach LOADER'}")
            _session_log.end_phase()

    # Mode 2 (2a/2b): collect node-management network info for THIS node
    # up-front (before option 4 runs) so the join wizard prompts can be
    # auto-answered from the config file or operator-entered values, matching
    # mode 1b's behavior. Mode 2a captures it for log fidelity even though
    # it won't be auto-applied.
    if _operation_mode == 2:
        # Ensure the cluster management IP is known before the join wizard
        # runs — avoids the mid-session prompt inside _fetch_existing_cluster_ip.
        # Only prompt when mode 2 was the original selection; mid-run
        # transitions (e.g. after 1b) already have mgmt_ip populated.
        if _initial_operation_mode == 2 and not _cluster_config.get("mgmt_ip"):
            cfg_cluster_2 = (_config_data.get("cluster") or {}) if isinstance(_config_data, dict) else {}
            _pre_mgmt_ip = cfg_cluster_2.get("clus_mgmt_address") or ""
            if not _pre_mgmt_ip:
                _print_banner("\U0001f4e1 Existing Cluster Details")
                try:
                    print("  " + "─" * 58)
                    _pre_mgmt_ip = input(
                        "\n  Cluster management IP (needed to look up the "
                        "cluster-network IP): "
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    _pre_mgmt_ip = ""
            if _pre_mgmt_ip:
                _cluster_config["mgmt_ip"] = _pre_mgmt_ip
                _session_log.log(f"Mode 2: cluster mgmt IP set up-front: {_pre_mgmt_ip}")

        # Ensure cluster admin credentials are known upfront so
        # _fetch_existing_cluster_ip() can authenticate silently during
        # the join wizard (avoids the mid-run "no credentials found" prompt).
        if _initial_operation_mode == 2:
            cfg_cluster_2 = (_config_data.get("cluster") or {}) if isinstance(_config_data, dict) else {}
            _has_creds = (
                _cluster_config.get("admin_user") and _cluster_config.get("admin_password")
            ) or (
                cfg_cluster_2.get("user") and cfg_cluster_2.get("password")
            ) or (
                sp_pass  # BMC creds are always a fallback candidate
            )
            if not _has_creds:
                _print_banner("\U0001f510 Existing Cluster Admin Credentials")
                print("\n  These are needed to look up the cluster-network IP")
                print("  during the join wizard. Enter blank to use the BMC")
                print("  credentials as a fallback.")
                try:
                    _pre_cl_user = input(
                        "\n  Cluster admin username [admin]: "
                    ).strip() or "admin"
                    _pre_cl_pass = getpass.getpass(
                        "  Cluster admin password (blank = use BMC password): "
                    )
                except (EOFError, KeyboardInterrupt):
                    _pre_cl_user = "admin"
                    _pre_cl_pass = ""
                if _pre_cl_pass:
                    _cluster_config["admin_user"] = _pre_cl_user
                    _cluster_config["admin_password"] = _pre_cl_pass
                    _session_log.log(
                        f"Mode 2: cluster admin credentials collected upfront "
                        f"(user={_pre_cl_user})"
                    )
                else:
                    _session_log.log(
                        "Mode 2: no cluster admin password entered; "
                        "will fall back to BMC credentials"
                    )

        collect_node_mgmt_per_bmc(sp_host, [])

    # ── Mode 2b multi-node: run parallel add when config has >1 secondary ──
    # If the config file lists multiple secondary nodes AND the operator
    # chose 2b (auto_add), collect all peers, confirm, and run every node
    # through LOADER → option 4 → join in parallel (joins serialized).
    # Single-node 2b falls through to the existing sequential path below.
    if _operation_mode == 2 and _auto_add:
        _2b_extra_peers = []
        if isinstance(_config_data, dict):
            _sn_list = _config_data.get("secondary_nodes")
            if isinstance(_sn_list, list):
                _2b_extra_peers = [
                    str(n["bmc"]) for n in _sn_list
                    if isinstance(n, dict) and n.get("bmc")
                    and str(n["bmc"]) != sp_host
                ]
            else:
                _all_nodes_2b = _config_data.get("nodes") or []
                _2b_extra_peers = [
                    str(n["bmc"]) for n in _all_nodes_2b
                    if isinstance(n, dict) and n.get("bmc")
                    and str(n["bmc"]) != sp_host
                ]
        if _2b_extra_peers:
            _2b_all_peers = [sp_host] + _2b_extra_peers

            # Collect node-mgmt info for each extra peer.
            for _ep in _2b_extra_peers:
                collect_node_mgmt_per_bmc(_ep, [])

            # Register sp_host credentials so the thread can look them up.
            if sp_host not in _peer_bmc_creds:
                _peer_bmc_creds[sp_host] = {"user": sp_user, "password": sp_pass}

            # Close the already-open channel for sp_host; _add_peer_node_thread
            # will establish its own fresh BMC connection for every peer.
            try:
                channel.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

            ok = _run_2b_parallel_add(
                _2b_all_peers, sp_user,
                {ip: (_peer_bmc_creds.get(ip) or {}).get("password", "")
                 for ip in _2b_all_peers},
                _session_log,
            )
            _session_log.record_completion(normal_exit=ok)
            print(f"\n📝 Session log: {_session_log.log_file}")
            sys.exit(0 if ok else 1)

    # If we captured retain data from an existing cluster (and the operator
    # didn't load a JSON config off disk), persist the merged in-memory
    # config to the default auto-detect location so the next run picks it
    # up automatically instead of having to scrape the cluster again.
    retain_captured = any((
        _retained_cluster_name,
        _retained_net_config,
        _retained_default_gateway,
        _retained_cluster_contact,
        _retained_cluster_location,
        _retained_dns_domains,
        _retained_dns_servers,
        _retained_sp_to_node,
    ))
    if retain_captured and not config_path:
        try:
            snap_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")
        except NameError:
            snap_dir = os.path.join(os.getcwd(), "configs")
        os.makedirs(snap_dir, exist_ok=True)
        snap_path = os.path.join(snap_dir, "reinit-config.json")
        write_config_snapshot(snap_path)

    # ── Option 3 checkpoint init (before destructive ops) ────────────────
    # The actual primary-setup phases are not skipped on resume (re-running
    # system-reset on a primary whose cluster is already up would be
    # destructive); on resume we detect primary_setup_done and steer the
    # operator to option 2c instead. Per-peer skip in add_peer_nodes_parallel
    # still prevents re-joining nodes that already completed.
    if _operation_mode == 3 and _checkpoint is None:
        _option3_init_checkpoint(_run_context, sp_host, _peer_bmc_list, config_path)

    # Phase: System Reset
    _session_log.start_phase("System Reset")
    print("\n🔄 Sending 'system reset' command...")
    _session_log.log("Sending 'system reset' command")
    direct_send_and_wait(channel, "system reset", "y/n", timeout=15, auto_respond="y")

    print("\n⏳ System reset in process. Script may appear hung, but be"
          " patient — reboot will happen soon.")
    _session_log.log("System reset issued; waiting for reboot")
    time.sleep(3)

    print("Waiting for BMC prompt after reset...")
    _session_log.log("Waiting for BMC prompt after reset")
    output = direct_read_until(channel, ">", timeout=15)
    if ">" in output:
        print("✅ BMC prompt returned.")
        _session_log.log("BMC prompt returned after reset")
    else:
        print("⚠️  BMC prompt not seen after reset, continuing anyway...")
        _session_log.log("BMC prompt not seen after reset", prefix="WARN")
    _session_log.end_phase()

    # Phase: Enter System Console
    _session_log.start_phase("Enter System Console")
    enter_system_console(channel)
    print("Now monitoring boot output...\n")
    _session_log.log("Starting boot monitoring")
    _session_log.end_phase()

    # Install per-node log writer so detailed boot/init output goes to a
    # dedicated file instead of flooding the terminal.  Milestone lines
    # (✅ / ⚠️ / 🤖 etc.) are still echoed to the screen.  Interactive
    # modes (1a/2a) run in pass-through so the operator sees everything.
    _is_auto_mode = _auto_setup or _auto_add or _operation_mode == 3
    _nlw_log_dir = _session_log.log_dir if _session_log else os.getcwd()
    _nlw_node_file = _node_log_open(
        sp_host, _nlw_log_dir,
        prefix="option2b_add_node" if _operation_mode == 2 else f"mode{_operation_mode}_node",
    )
    _nlw = _NodeLogWriter(_nlw_node_file, interactive=not _is_auto_mode)
    sys.stdout = _nlw
    _real_stdout.write(
        f"\n  📝 [{sp_host}] Detailed node output → {_nlw_node_file.name}\n"
    )
    _real_stdout.flush()

    # Phase: AUTOBOOT/LOADER Monitoring
    # (sub-phases LOADER Commands, Boot Menu, Interactive are handled inside)
    _session_log.start_phase("AUTOBOOT/LOADER Monitoring")
    monitor_for_autoboot_and_loader(channel, client, sp_host, sp_user, sp_pass)

    # Mode 2b: support adding additional nodes within the same run. The join
    # automation may set _add_another_node_request to (host, user, password)
    # to drive a fresh BMC through the same pipeline.
    while _add_another_node_request is not None:
        next_host, next_user, next_pass = _add_another_node_request
        _add_another_node_request = None
        _shutdown_event.clear()

        _print_banner(f"▶️  Adding next node: {next_host}")
        _slog(f"Switching to next node: {next_host}")

        try:
            channel.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass

        sp_host, sp_user, sp_pass = next_host, next_user, next_pass

        _session_log.start_phase(f"SSH Connection ({sp_host})")
        client, sp_user, sp_pass = connect_to_sp(sp_host, sp_user, sp_pass)
        channel = _open_shell(client)
        threading.Thread(
            target=keepalive_loop, args=(client,), daemon=True
        ).start()
        _session_log.log("Keepalive thread started for next node")
        _session_log.end_phase()

        _session_log.start_phase(f"BMC Prompt ({sp_host})")
        if not wait_for_bmc_prompt(channel):
            print(f"⚠️  Could not reach BMC prompt on {sp_host}; aborting.")
            _session_log.log(f"BMC prompt timeout on {sp_host}; aborting next-node",
                             prefix="ERROR")
            _session_log.end_phase()
            break
        drain_channel(channel, seconds=1)
        _session_log.end_phase()

        # Collect node-management info for this new BMC up front.
        collect_node_mgmt_per_bmc(sp_host, [])

        _session_log.start_phase(f"System Reset ({sp_host})")
        print("\n🔄 Sending 'system reset' command...")
        _session_log.log("Sending 'system reset' command")
        direct_send_and_wait(channel, "system reset", "y/n", timeout=15,
                             auto_respond="y")
        print("\n⏳ System reset in process. Script may appear hung, but"
              " be patient — reboot will happen soon.")
        _session_log.log("System reset issued; waiting for reboot")
        time.sleep(3)
        direct_read_until(channel, ">", timeout=15)
        _session_log.end_phase()

        _session_log.start_phase(f"Enter System Console ({sp_host})")
        enter_system_console(channel)
        print("Now monitoring boot output...\n")
        _session_log.log("Starting boot monitoring for next node")
        _session_log.end_phase()

        # Close the previous node's log writer and open a fresh one for this node.
        if isinstance(sys.stdout, _NodeLogWriter):
            try:
                sys.stdout._nf.close()
            except Exception:
                pass
        _nlw_node_file2 = _node_log_open(
            sp_host, _nlw_log_dir,
            prefix="option2b_add_node" if _operation_mode == 2 else f"mode{_operation_mode}_node",
        )
        _nlw2 = _NodeLogWriter(_nlw_node_file2, interactive=not _is_auto_mode)
        sys.stdout = _nlw2
        _real_stdout.write(
            f"\n  📝 [{sp_host}] Detailed node output → {_nlw_node_file2.name}\n"
        )
        _real_stdout.flush()

        _session_log.start_phase(f"AUTOBOOT/LOADER Monitoring ({sp_host})")
        monitor_for_autoboot_and_loader(channel, client, sp_host, sp_user, sp_pass)

    # Restore real stdout and close per-node log file.
    if isinstance(sys.stdout, _NodeLogWriter):
        try:
            sys.stdout._nf.close()
        except Exception:
            pass
        sys.stdout = _real_stdout

    # Cleanup
    _shutdown_event.set()
    print("⏳ Cleaning up... (press Ctrl+C again to force exit)")
    if _session_log:
        _session_log.end_phase()
        _session_log.log("Shutting down")

    try:
        channel.close()
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass

    print("🔒 SSH session closed.")
    if _session_log:
        _session_log.log("SSH session closed")
        _session_log.record_completion(normal_exit=True)
        print(f"\n📝 Full session log saved to: {_session_log.log_file}")


if __name__ == "__main__":
    main()
