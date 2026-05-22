#!/usr/bin/env python3

# ---------------------------------------------------------------------------
# Suppress CryptographyDeprecationWarning BEFORE any other imports
# ---------------------------------------------------------------------------
import warnings
warnings.filterwarnings(
    "ignore",
    message=r".*Python .* is no longer supported.*",
    category=DeprecationWarning,
)

import subprocess
import sys
import os
import time
import re
import getpass
import logging
import threading
import signal
import argparse
import platform
import socket
from datetime import datetime

# ---------------------------------------------------------------------------
# Session logging with phase timing
# ---------------------------------------------------------------------------

class SessionLogger:
    def __init__(self):
        self.log_dir = os.path.join(os.getcwd(), "bmc_session_logs")
        os.makedirs(self.log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(self.log_dir, f"bmc_session_{timestamp}.log")
        self._lock = threading.Lock()
        self._file = open(self.log_file, "w", encoding="utf-8")
        self._start_time = datetime.now()
        self._phase_times = {}
        self._current_phase = None
        self._current_phase_start = None
        self._write_header()
        print(f"📝 Session log: {self.log_file}")

    def _write_header(self):
        self._file.write("=" * 70 + "\n")
        self._file.write(f"BMC Session Log\n")
        self._file.write(f"Started: {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._file.write(f"Host: {platform.node()}\n")
        self._file.write(f"Python: {sys.version}\n")
        self._file.write("=" * 70 + "\n\n")
        self._file.flush()

    def start_phase(self, phase_name):
        with self._lock:
            now = datetime.now()
            if self._current_phase and self._current_phase_start:
                elapsed = (now - self._current_phase_start).total_seconds()
                self._phase_times[self._current_phase] = elapsed
            self._current_phase = phase_name
            self._current_phase_start = now
            ts = now.strftime("%H:%M:%S.%f")[:-3]
            self._file.write(f"\n[{ts}] [PHASE] ▶ Started: {phase_name}\n")
            self._file.flush()

    def end_phase(self):
        with self._lock:
            if self._current_phase and self._current_phase_start:
                now = datetime.now()
                elapsed = (now - self._current_phase_start).total_seconds()
                self._phase_times[self._current_phase] = elapsed
                ts = now.strftime("%H:%M:%S.%f")[:-3]
                self._file.write(f"[{ts}] [PHASE] ⏹ Ended: {self._current_phase} ({elapsed:.1f}s)\n\n")
                self._file.flush()
                self._current_phase = None
                self._current_phase_start = None

    def log(self, message, prefix="INFO"):
        with self._lock:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self._file.write(f"[{ts}] [{prefix}] {message}\n")
            self._file.flush()

    def log_console(self, data):
        with self._lock:
            self._file.write(data)
            self._file.flush()

    def log_user_input(self, data):
        with self._lock:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self._file.write(f"[{ts}] [USER_INPUT] {data}\n")
            self._file.flush()

    def log_sent(self, data):
        with self._lock:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            display = repr(data) if any(ord(c) < 32 and c not in '\r\n' for c in data) else data.strip()
            self._file.write(f"[{ts}] [SENT] {display}\n")
            self._file.flush()

    def close(self):
        with self._lock:
            if self._current_phase and self._current_phase_start:
                elapsed = (datetime.now() - self._current_phase_start).total_seconds()
                self._phase_times[self._current_phase] = elapsed

            total_elapsed = (datetime.now() - self._start_time).total_seconds()

            self._file.write(f"\n{'=' * 70}\n")
            self._file.write(f"Session ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._file.write(f"Total runtime: {total_elapsed:.1f}s ({total_elapsed/60:.1f} minutes)\n")
            self._file.write(f"\n{'─' * 70}\n")
            self._file.write(f"Phase Timing Summary\n")
            self._file.write(f"{'─' * 70}\n")
            for phase, elapsed in self._phase_times.items():
                minutes = elapsed / 60
                self._file.write(f"  {phase:<45} {elapsed:>7.1f}s ({minutes:.1f}m)\n")
            self._file.write(f"  {'─' * 55}\n")
            self._file.write(f"  {'TOTAL':<45} {total_elapsed:>7.1f}s ({total_elapsed/60:.1f}m)\n")
            self._file.write("=" * 70 + "\n")
            self._file.close()


_session_log = None

# ---------------------------------------------------------------------------
# Operation mode selection
# ---------------------------------------------------------------------------

_operation_mode = None


def select_operation_mode():
    while True:
        print("\n" + "=" * 60)
        print("  NetApp AFX BMC Console Automation 🤖")
        print("=" * 60)
        print("\n  What do you want to do?\n")
        print("  1) Initialize the first node in an AFX cluster")
        print("  2) Initialize a node to be added to an existing AFX cluster")
        print("  3) Exit this script")
        print("")

        choice = input("  Enter your choice (1, 2, or 3): ").strip()

        if choice == "1":
            print("\n" + "=" * 60)
            print("  ⚠️  WARNING ⚠️")
            print("=" * 60)
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
            print("  Do you want to continue?")
            print("")
            print("  Yes - continue reinit")
            print("  No  - go back to menu")
            print("")

            confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
            if confirm == "yes":
                print("\n  ✅ Confirmed. Mode 1 selected: Initialize first node")
                print("     → LOADER: set-defaults + destroy storage pods + saveenv")
                print("     → Boot menu: option 9 (Initialize)\n")
                return 1
            else:
                print("\n  ↩️  Returning to menu...\n")
                continue

        elif choice == "2":
            print("\n" + "=" * 60)
            print("  ⚠️  NOTICE ⚠️")
            print("=" * 60)
            print("")
            print("  " + "*" * 58)
            print("  * CAUTION: YOU HAVE SELECTED OPTION 2, WHICH FORMATS  *")
            print("  * AND JOINS AN AFX NODE TO AN EXISTING CLUSTER. IF    *")
            print("  * THE CLUSTER DOES NOT EXIST ALREADY, CHOOSE NO AND   *")
            print("  * SELECT OPTION 1.                                     *")
            print("  " + "*" * 58)
            print("")
            print("  Do you want to continue?")
            print("")
            print("  Yes - continue with add node")
            print("  No  - go back to menu")
            print("")

            confirm = input("  Enter 'yes' to continue or 'no' to go back: ").strip().lower()
            if confirm == "yes":
                print("\n  ✅ Confirmed. Mode 2 selected: Add node to existing cluster")
                print("     → LOADER: set-defaults + saveenv (no destroy storage pods)")
                print("     → Boot menu: option 4 (Initialize and configure system)\n")
                return 2
            else:
                print("\n  ↩️  Returning to menu...\n")
                continue

        elif choice == "3":
            print("\n  👋 Exiting script. No changes were made.")
            sys.exit(0)

        else:
            print("  ⚠️  Invalid choice. Please enter 1, 2, or 3.")


def get_loader_commands():
    if _operation_mode == 1:
        return [
            "set-defaults",
            "setenv bootarg.destroy.all.storage.pods true",
            "saveenv",
            "boot_ontap menu"
        ]
    else:
        return [
            "set-defaults",
            "saveenv",
            "boot_ontap menu"
        ]


def get_boot_menu_option():
    if _operation_mode == 1:
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
        f"   Install it now using '{pkg_manager}'? [y/N]: "
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
            __import__(module_name)
            continue
        except ImportError:
            pass
        if pkg_manager and pkg_manager in pkg_info:
            print(f"Module '{module_name}' is missing.")
            install_system_package(pkg_info[pkg_manager], pkg_manager)
            try:
                __import__(module_name)
                continue
            except ImportError:
                print(f"⚠️  System package installed but module still not importable. "
                      f"Falling back to pip.")
        answer = input(
            f"⚠️  Python module '{module_name}' is not installed.\n"
            f"   Install it now via pip? [y/N]: "
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

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()
_client_lock    = threading.Lock()
_active_client  = None
_ctrl_c_count   = 0

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


def connect_to_sp(host, username, password):
    global _active_client
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        print(f"Connecting to SP at {host} with username {username}...")
        if _session_log:
            _session_log.log(f"Connecting to SP at {host} with username {username}")
        client.connect(hostname=host, username=username, password=password,
                        timeout=30, banner_timeout=30, auth_timeout=30)
        configure_transport(client)
        print("✅ Connection successful!")
        if _session_log:
            _session_log.log("SSH connection established successfully")
        with _client_lock:
            _active_client = client
        return client
    except Exception as e:
        print(f"❌ Error connecting to SP: {e}")
        if _session_log:
            _session_log.log(f"SSH connection failed: {e}", prefix="ERROR")
        sys.exit(1)


def is_session_alive(client, channel):
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            return False
        return not channel.closed
    except Exception:
        return False


def reconnect_to_sp(host, username, password):
    global _active_client
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        print(f"\n🔄 Reconnection attempt {attempt}/{max_retries}...")
        if _session_log:
            _session_log.log(f"Reconnection attempt {attempt}/{max_retries}")
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(hostname=host, username=username, password=password,
                            timeout=30, banner_timeout=30, auth_timeout=30)
            configure_transport(client)
            channel = client.invoke_shell()
            channel.settimeout(0)
            print("✅ Reconnected!")
            if _session_log:
                _session_log.log("Reconnected successfully")
            with _client_lock:
                _active_client = client
            return client, channel
        except Exception as e:
            print(f"⚠️  Attempt {attempt} failed: {e}")
            if _session_log:
                _session_log.log(f"Reconnection attempt {attempt} failed: {e}", prefix="ERROR")
            time.sleep(5)
    print("❌ Could not reconnect after multiple attempts.")
    if _session_log:
        _session_log.log("All reconnection attempts failed", prefix="ERROR")
    return None, None


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

def direct_send_and_wait(channel, command, look_for, timeout=15, auto_respond=None):
    logger = logging.getLogger(__name__)
    if _session_log and command:
        _session_log.log_sent(command)
    if command:
        channel.send(command + "\r")

    output = ""
    start_time = time.time()
    while True:
        if _shutdown_event.is_set():
            return output
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            sys.stdout.write(chunk)
            sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            if look_for and look_for.lower() in output.lower():
                if auto_respond:
                    time.sleep(0.3)
                    channel.send(auto_respond + "\r")
                    print(f"\n✅ Detected '{look_for}' – auto-responded with '{auto_respond}'")
                    if _session_log:
                        _session_log.log(f"Detected '{look_for}' – auto-responded with '{auto_respond}'")
                        _session_log.log_sent(auto_respond)
                return output
        if time.time() - start_time > timeout:
            if _session_log:
                _session_log.log(f"Timeout ({timeout}s) waiting for '{look_for}'", prefix="WARN")
            return output
        time.sleep(0.05)


def direct_read_until(channel, look_for, timeout=15):
    output = ""
    start_time = time.time()
    while True:
        if _shutdown_event.is_set():
            return output
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            sys.stdout.write(chunk)
            sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            if look_for and look_for.lower() in output.lower():
                return output
        if time.time() - start_time > timeout:
            if _session_log:
                _session_log.log(f"Timeout ({timeout}s) waiting for '{look_for}'", prefix="WARN")
            return output
        time.sleep(0.05)


def direct_read_until_any(channel, look_for_list, timeout=15):
    output = ""
    start_time = time.time()
    while True:
        if _shutdown_event.is_set():
            return output, None
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            sys.stdout.write(chunk)
            sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
            output_lower = output.lower()
            for look_for in look_for_list:
                if look_for.lower() in output_lower:
                    return output, look_for
        if time.time() - start_time > timeout:
            if _session_log:
                _session_log.log(f"Timeout ({timeout}s) waiting for any of {look_for_list}", prefix="WARN")
            return output, None
        time.sleep(0.05)


def drain_channel(channel, seconds=2):
    output = ""
    start_time = time.time()
    while time.time() - start_time < seconds:
        if _shutdown_event.is_set():
            return output
        if channel.recv_ready():
            chunk = channel.recv(4096).decode("utf-8", errors="replace")
            output += chunk
            sys.stdout.write(chunk)
            sys.stdout.flush()
            if _session_log:
                _session_log.log_console(chunk)
        time.sleep(0.05)
    return output


# ---------------------------------------------------------------------------
# Signal handler – force exit on second Ctrl+C
# ---------------------------------------------------------------------------

def signal_handler(sig, frame):
    global _ctrl_c_count
    _ctrl_c_count += 1

    if _ctrl_c_count == 1:
        print("\n👋 Received termination signal. Cleaning up...")
        if _session_log:
            _session_log.log("Received termination signal (Ctrl+C or SIGTERM)")
        _shutdown_event.set()
    else:
        print("\n⚡ Force exit!")
        if _session_log:
            try:
                _session_log.log("Force exit (second Ctrl+C)")
                _session_log.close()
            except Exception:
                pass
        os._exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="NetApp AFX BMC console automation script 🤖"
    )
    parser.add_argument("--debug", "-d", action="store_true", default=False,
                        help="Enable debug logging (off by default)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Wait for BMC prompt
# ---------------------------------------------------------------------------

def wait_for_bmc_prompt(channel):
    print("Shell invoked. Waiting for initial prompt...")
    if _session_log:
        _session_log.log("Waiting for initial BMC prompt (watching for existing session y/n)")

    output, matched = direct_read_until_any(channel, ["y/n", ">"], timeout=15)

    if matched and "y/n" in matched.lower():
        print("\n⚠️  An existing session is active on this BMC!")
        answer = input("   Do you want to disconnect the other session? [y/n]: ").strip().lower()
        if _session_log:
            _session_log.log_user_input(f"Existing session takeover response: {answer}")

        if answer == "y":
            print("Disconnecting other session...")
            if _session_log:
                _session_log.log("User chose to take over existing session")
                _session_log.log_sent("y")
            channel.send("y\r")
            time.sleep(2)

            output = direct_read_until(channel, ">", timeout=15)
            if ">" not in output:
                print("❌ Did not receive BMC prompt after session takeover. Exiting.")
                if _session_log:
                    _session_log.log("BMC prompt not received after session takeover", prefix="ERROR")
                return False
            print("✅ BMC prompt detected after session takeover.")
            if _session_log:
                _session_log.log("BMC prompt detected after session takeover")
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
        if _session_log:
            _session_log.log("BMC prompt detected (no existing session)")
        return True

    else:
        print("❌ Did not receive BMC prompt. Exiting.")
        if _session_log:
            _session_log.log("BMC prompt not received – timeout", prefix="ERROR")
        return False


# ---------------------------------------------------------------------------
# Enter system console
# ---------------------------------------------------------------------------

def enter_system_console(channel):
    print("\n📺 Entering system console...")
    if _session_log:
        _session_log.log("Entering system console")
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
        answer = input("   Do you want to disconnect the other console session? [y/n]: ").strip().lower()
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
                if _session_log:
                    _session_log.log("System console connected after session takeover")
            else:
                print("⚠️  No console confirmation after takeover, continuing anyway...")
                if _session_log:
                    _session_log.log("No console confirmation after takeover", prefix="WARN")
        else:
            print("❌ Cannot continue without console access. Exiting.")
            if _session_log:
                _session_log.log("User declined to take over console session – exiting")
                _session_log.log_sent("n")
            channel.send("n\r")
            sys.exit(1)

    elif matched:
        print("✅ System console connected.")
        if _session_log:
            _session_log.log(f"System console connected (matched: {matched})")
    else:
        print("⚠️  No console confirmation detected, continuing anyway...")
        if _session_log:
            _session_log.log("No console confirmation detected", prefix="WARN")

    drain_channel(channel, seconds=3)
    print("✅ System console ready.\n")
    if _session_log:
        _session_log.log("System console ready")


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
        if _session_log:
            _session_log.log("SSH dropped during interactive session, reconnecting")
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
        if _session_log:
            _session_log.log("Reattached to system console after reconnect")
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
                    time.sleep(0.05)
            except Exception as e:
                if _session_log:
                    _session_log.log(f"Reader error: {e}", prefix="ERROR")
                if not self._try_reconnect():
                    break

    def run(self):
        print("\n📺 Session is now fully interactive.")
        print("   Type your responses to any prompts (yes, no, etc.)")
        print("   ⚠️  AUTOBOOT messages are NORMAL from this point – they will NOT be interrupted.")
        print("   Press Ctrl+C to exit. (Press twice to force exit.)\n")
        if _session_log:
            _session_log.log("Entered interactive session (Phase 3 – passive mode)")

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

        print("\n👋 Exiting interactive session.")
        if _session_log:
            _session_log.log("Exited interactive session")


# ---------------------------------------------------------------------------
# Boot menu handler
# ---------------------------------------------------------------------------

def wait_for_boot_menu_and_select(channel):
    option, description = get_boot_menu_option()

    print(f"\n⏳ Waiting for boot menu to appear...")
    if _session_log:
        _session_log.log(f"Phase 2: Waiting for boot menu (will auto-select option {option} – {description})")

    output = direct_read_until(channel, "please choose one of the following", timeout=180)
    if "please choose one of the following" not in output.lower():
        print("⚠️  Boot menu prompt not detected within timeout.")
        if _session_log:
            _session_log.log("Boot menu prompt not detected within timeout", prefix="WARN")
        return False

    print("")
    drain_channel(channel, seconds=5)

    print(f"\n🔢 Boot menu detected! Automatically selecting option {option} ({description})...")
    if _session_log:
        _session_log.log(f"Boot menu detected – auto-selecting option {option} ({description})")
        _session_log.log_sent(option)

    channel.send(option + "\r")
    print(f"✅ Option {option} sent.\n")
    time.sleep(2)
    return True


# ---------------------------------------------------------------------------
# LOADER / boot-menu handler
# ---------------------------------------------------------------------------

def handle_loader_commands(channel, client, sp_host, sp_user, sp_pass):
    print("\n✅ Detected LOADER prompt. Running required commands...")
    if _session_log:
        _session_log.end_phase()  # End AUTOBOOT/LOADER Monitoring
        _session_log.start_phase("LOADER Commands")
        _session_log.log("LOADER prompt detected – running boot configuration commands")

    drain_channel(channel, seconds=1)

    print("\nGetting fresh LOADER prompt...")
    output = direct_send_and_wait(channel, "", "LOADER", timeout=15)
    if "loader" not in output.lower():
        print("⚠️  No LOADER prompt seen, attempting commands anyway...")

    loader_commands = get_loader_commands()

    if _session_log:
        _session_log.log(f"LOADER commands for mode {_operation_mode}: {loader_commands}")

    for command in loader_commands:
        print(f"\nRunning command: {command}")
        if _session_log:
            _session_log.log(f"Running LOADER command: {command}")
        if command != "boot_ontap menu":
            output = direct_send_and_wait(channel, command, "LOADER", timeout=15)
            if "loader" not in output.lower():
                print(f"⚠️  No LOADER prompt after '{command}', continuing anyway...")
        else:
            channel.send(command + "\r")
            if _session_log:
                _session_log.log_sent(command)
            time.sleep(1)

    if _session_log:
        _session_log.end_phase()  # End LOADER Commands
        _session_log.start_phase("Boot Menu Selection")

    if not wait_for_boot_menu_and_select(channel):
        print("\n⚠️  Falling back to manual menu selection...")
        if _session_log:
            _session_log.log("Auto-select failed, falling back to manual input", prefix="WARN")
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
    if _session_log:
        _session_log.log("Phase 1: Monitoring for AUTOBOOT/LOADER (active interruption mode)")

    output_buffer = ""

    try:
        while not _shutdown_event.is_set():
            if not is_session_alive(client, channel):
                print("\n⚠️  Session dropped during monitoring. Reconnecting...")
                if _session_log:
                    _session_log.log("Session dropped during monitoring", prefix="WARN")
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
                    if _session_log:
                        _session_log.log("AUTOBOOT detected – sending Ctrl+C to interrupt")
                    for _ in range(5):
                        channel.send("\x03")
                        time.sleep(0.3)
                    print("✅ Ctrl+C sent.")
                    if _session_log:
                        _session_log.log("Ctrl+C sent to interrupt AUTOBOOT")
                    output_buffer = ""

                elif re.search(r'LOADER-\w+>', output_buffer):
                    if _session_log:
                        _session_log.log("LOADER prompt detected")
                    handle_loader_commands(channel, client, sp_host, sp_user, sp_pass)
                    break

                if len(output_buffer) > 8192:
                    output_buffer = output_buffer[-4096:]

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n👋 User interrupted. Exiting...")
        if _session_log:
            _session_log.log("User interrupted during monitoring (Ctrl+C)")
    except (OSError, EOFError, paramiko.SSHException) as e:
        print(f"\n⚠️  Connection error during monitoring: {e}")
        if _session_log:
            _session_log.log(f"Connection error during monitoring: {e}", prefix="ERROR")
        print("Press Ctrl+C to exit...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _session_log, _operation_mode

    args = parse_args()
    setup_logging(args.debug)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    _operation_mode = select_operation_mode()

    _session_log = SessionLogger()

    if _operation_mode == 1:
        mode_desc = "Initialize first node (option 9, destroy storage pods)"
    else:
        mode_desc = "Add node to existing cluster (option 4, initialize and configure system)"
    _session_log.log(f"Operation mode: {_operation_mode} – {mode_desc}")

    sp_host = input("Enter SP hostname/IP: ")
    sp_user = input("Enter SP username: ")
    sp_pass = getpass.getpass("Enter SP password: ")

    _session_log.log(f"Target BMC: {sp_host}")
    _session_log.log(f"Username: {sp_user}")
    _session_log.log(f"Debug mode: {args.debug}")

    # Phase: SSH Connection
    _session_log.start_phase("SSH Connection")
    client = connect_to_sp(sp_host, sp_user, sp_pass)
    channel = client.invoke_shell()
    channel.settimeout(0)

    keepalive_thread = threading.Thread(
        target=keepalive_loop, args=(client,), daemon=True
    )
    keepalive_thread.start()
    _session_log.log("Keepalive thread started")
    _session_log.end_phase()

    # Phase: BMC Prompt & Validation
    _session_log.start_phase("BMC Prompt & Validation")
    if not wait_for_bmc_prompt(channel):
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
        answer = input("\n   Does this look like the correct BMC? [y/N]: ").strip().lower()
        _session_log.log_user_input(f"BMC validation confirmation: {answer}")
        if answer != "y":
            print("❌ BMC validation rejected. Exiting.")
            _session_log.log("BMC validation rejected", prefix="ERROR")
            _session_log.close()
            sys.exit(1)
        print("✅ BMC validation confirmed by user.")
        _session_log.log("BMC validation confirmed by user")
    _session_log.end_phase()

    # Phase: System Reset
    _session_log.start_phase("System Reset")
    print("\n🔄 Sending 'system reset' command...")
    _session_log.log("Sending 'system reset' command")
    direct_send_and_wait(channel, "system reset", "y/n", timeout=15, auto_respond="y")

    print("\n⏳ Waiting 3 seconds for reset to begin...")
    _session_log.log("Waiting 3 seconds for reset to begin")
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

    # Phase: AUTOBOOT/LOADER Monitoring
    # (sub-phases LOADER Commands, Boot Menu, Interactive are handled inside)
    _session_log.start_phase("AUTOBOOT/LOADER Monitoring")
    monitor_for_autoboot_and_loader(channel, client, sp_host, sp_user, sp_pass)

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
        print(f"\n📝 Full session log saved to: {_session_log.log_file}")
        _session_log.close()


if __name__ == "__main__":
    main()
