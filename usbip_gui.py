#!/usr/bin/env python3
"""
usbip_gui.py -- minimal cross-platform front-end for usbipd-win / usbip.

Run one copy on each machine; it auto-detects the OS.

  * Windows host        -> list / Share / Unshare / force-Detach USB devices.
  * Ubuntu (Hyper-V VM) -> list remote devices, Attach / Detach, and keep a
                           chosen device attached across BeagleBone reboots.

IMPORTANT -- run the Linux side as your normal user, NOT with sudo.
`sudo python3 ...` over SSH breaks X11 ("couldn't connect to display").
Give usbip passwordless sudo instead -- see the Help / Setup button.

What this app runs, and nothing else:
  usbip, usbipd, modprobe, ip route.  It does not change the Windows firewall,
  registry, services, or any file -- except the clearly-labelled buttons in the
  Help window.

Standard library only (Tkinter ships with CPython):  python3 usbip_gui.py

Author: Yassine El Harami -- MIT License (see LICENSE)
"""

__author__ = "Yassine El Harami"

import glob
import json
import os
import platform
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
DEFAULT_HOST     = ""          # blank -> auto-detect the Hyper-V host (gateway)
POLL_SEC         = 2.0         # auto-attach poll interval (clamped >= 1.0)
DEFAULT_SUDO     = "sudo -n"   # prefix for usbip/modprobe on Linux
USBIPD           = "usbipd"    # usbipd(.exe) on Windows
USBIP            = "usbip"     # usbip on Linux (auto-resolved at startup)
MODPROBE         = "modprobe"  # modprobe on Linux (auto-resolved at startup)
CMD_TIMEOUT      = 15          # seconds per shell command
LIST_TIMEOUT     = 5           # seconds for `usbip list -r` (host probe)
AUTOREFRESH_SEC  = 5           # background device-list refresh
MISSING_BACKOFF  = 4.0         # gap between attach tries while a board is down
BUSY_BACKOFF     = 15.0        # gap when the host reports a stale attachment
GRACE_SEC        = 6.0         # trust a fresh successful attach for this long
CONFIRM_ATTACH   = True        # ask before attaching a device to the VM

IS_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
if not IS_WINDOWS and hasattr(os, "geteuid") and os.geteuid() == 0:
    DEFAULT_SUDO = ""


# --------------------------------------------------------------------------
# shell helper -- logs the commands it runs unless quiet=True
# --------------------------------------------------------------------------
_LOG = None
_warned_sudo = False


def set_logger(fn):
    global _LOG
    _LOG = fn


def _log(msg):
    if _LOG:
        _LOG(str(msg))


def last_line(*chunks):
    text = "\n".join(c for c in chunks if c).strip()
    return text.splitlines()[-1] if text else ""


def run(args, timeout=CMD_TIMEOUT, quiet=False):
    """Run a command (list of args). Return (rc, stdout, stderr). Never raises."""
    global _warned_sudo
    args = [str(a) for a in args]
    if not quiet:
        _log("$ " + " ".join(shlex.quote(a) for a in args))
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, creationflags=_NO_WINDOW)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        rc, out, err = 127, "", "command not found: %s" % args[0]
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "timeout after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        rc, out, err = 1, "", str(e)
    if not quiet:
        _log("  → ok" if rc == 0 else "  → rc=%d  %s" % (rc, last_line(err, out)))
    if rc != 0 and not _warned_sudo and re.search(
            r"sudo:\s+(a password is required|a terminal is required|no tty)", err, re.I):
        _warned_sudo = True
        _log("  ! passwordless sudo is not set up -- see Help / Setup")
    return rc, out, err


# --------------------------------------------------------------------------
# host / tool detection (Linux)
# --------------------------------------------------------------------------
def detect_windows_host():
    """In a Hyper-V 'Default Switch' VM the Windows host is the default gateway
    (and the DNS server). Returns an IPv4 string or ''."""
    _, out, _ = run(["ip", "-4", "route", "show", "default"], timeout=5, quiet=True)
    m = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)", out)
    if m:
        return m.group(1)
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0] == "nameserver" and not p[1].startswith("127."):
                    return p[1]
    except OSError:
        pass
    return ""


def resolve_usbip():
    p = shutil.which("usbip")
    if p:
        return p
    for c in ("/usr/bin/usbip", "/usr/sbin/usbip"):
        if os.path.exists(c):
            return c
    rel = platform.uname().release
    cands = sorted(glob.glob("/usr/lib/linux-tools/*/usbip")
                   + glob.glob("/usr/lib/linux-tools-*/usbip"))
    for c in cands:
        if rel in c:
            return c
    return cands[-1] if cands else "usbip"


# --------------------------------------------------------------------------
# Windows backend -- usbipd-win
# --------------------------------------------------------------------------
class WindowsBackend:
    kind = "windows"

    def list_devices(self):
        rc, out, _ = run([USBIPD, "state"], quiet=True)
        if rc == 0 and out.strip():
            try:
                return self._parse_state(out)
            except Exception:
                pass
        rc, out, err = run([USBIPD, "list"], quiet=True)
        if rc != 0:
            raise RuntimeError(last_line(err, out) or "usbipd list failed")
        return self._parse_list(out)

    @staticmethod
    def _vidpid(instance_id):
        m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", instance_id or "")
        return "%s:%s" % (m.group(1).lower(), m.group(2).lower()) if m else ""

    def _parse_state(self, out):
        devs = []
        for d in json.loads(out).get("Devices", []):
            busid = d.get("BusId") or ""
            if not busid:
                continue
            if d.get("ClientIPAddress"):
                state = "Attached " + str(d["ClientIPAddress"])
            elif d.get("PersistedGuid") or d.get("IsForced"):
                state = "Shared" + (" (forced)" if d.get("IsForced") else "")
            else:
                state = "Not shared"
            devs.append({"busid": busid,
                         "vidpid": d.get("HardwareId") or self._vidpid(d.get("InstanceId")),
                         "desc": d.get("Description", ""), "state": state})
        return devs

    @staticmethod
    def _parse_list(out):
        devs, section = [], None
        for line in out.splitlines():
            s = line.strip(); low = s.lower()
            if low.startswith("connected:"):
                section = "connected"; continue
            if low.startswith("persisted:"):
                section = "persisted"; continue
            if section != "connected" or not s or s.upper().startswith("BUSID"):
                continue
            m = re.match(r"^(\S+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.*?)\s{2,}(\S.*)$",
                         line.rstrip())
            if m:
                b, v, d, st = m.groups()
                devs.append({"busid": b, "vidpid": v, "desc": d.strip(), "state": st.strip()})
        return devs

    def bind(self, busid, force=False):
        return run([USBIPD, "bind", "--busid", busid] + (["--force"] if force else []))

    def unbind(self, busid):
        return run([USBIPD, "unbind", "--busid", busid])

    def unbind_all(self):
        return run([USBIPD, "unbind", "--all"])

    def detach(self, busid):
        return run([USBIPD, "detach", "--busid", busid])

    def detach_all(self):
        return run([USBIPD, "detach", "--all"])


# --------------------------------------------------------------------------
# Linux backend -- usbip
# --------------------------------------------------------------------------
class LinuxBackend:
    kind = "linux"

    def __init__(self, sudo_str=DEFAULT_SUDO):
        self.sudo_str = sudo_str

    def sudo(self):
        return shlex.split(self.sudo_str) if self.sudo_str.strip() else []

    def load_module(self, quiet=False):
        return run(self.sudo() + [MODPROBE, "vhci-hcd"], quiet=quiet)

    def module_loaded(self):
        try:
            with open("/proc/modules") as f:
                return any(l.startswith("vhci_hcd") for l in f)
        except OSError:
            return True

    def list_remote(self, host, timeout=CMD_TIMEOUT):
        """Devices the host is currently exporting (bound/shared)."""
        if not host:
            raise RuntimeError("no host set -- press Detect or type the Windows host IP")
        rc, out, err = run([USBIP, "list", "-r", host], timeout=timeout, quiet=True)
        if rc != 0:
            raise RuntimeError(last_line(err, out) or "usbip list -r %s failed" % host)
        devs = []
        for line in out.splitlines():
            m = re.match(r"\s+(\d+-[\d.]+):\s+(.*?)\s+\((\w{4}):(\w{4})\)\s*$", line)
            if m:
                busid, name, vid, pid = m.groups()
                devs.append({"busid": busid, "vidpid": ("%s:%s" % (vid, pid)).lower(),
                             "desc": name.strip()})
        return devs

    @staticmethod
    def _vidpid_of_local(local_busid):
        """vid:pid of a locally-enumerated USB device, e.g. local busid 1-1."""
        base = "/sys/bus/usb/devices/%s" % local_busid
        try:
            with open(base + "/idVendor") as f:
                vid = f.read().strip()
            with open(base + "/idProduct") as f:
                pid = f.read().strip()
            return ("%s:%s" % (vid, pid)).lower()
        except OSError:
            return ""

    def read_ports(self):
        return run(self.sudo() + [USBIP, "port"], quiet=True)

    def list_attached(self, port_output=None):
        """Imported devices: [{port, local, host, busid, vidpid, desc}].

        `busid` is the REMOTE busid -- the one the host exports and that we
        attach by.  This matters: the vhci sysfs column is called `local_busid`
        and holds the *local* vhci id (1-1, 2-1 ...), NOT the remote one.  Only
        `usbip port` carries the mapping:

            1-1 -> usbip://172.26.0.1:3240/10-3
            ^local                        ^remote

        Comparing the local id against a remote id is what used to make the
        watcher believe an attached device was missing and re-attach it.
        """
        if port_output is None:
            _, port_output, _ = self.read_ports()
        att = []
        port, vidpid, desc = None, "", ""
        for line in port_output.splitlines():
            m = re.match(r"Port (\d+):", line.strip())
            if m:
                port, vidpid, desc = str(int(m.group(1))), "", ""
                continue
            m = re.match(r"\s+(.*?)\s+\((\w{4}):(\w{4})\)\s*$", line)
            if m:
                desc = m.group(1).strip()
                vidpid = ("%s:%s" % (m.group(2), m.group(3))).lower()
                continue
            m = re.search(
                r"(\S+)\s+->\s+(?:usbip://(\S+):\d+/(\S+)|"
                r"unknown host, remote port and remote busid)\s*$", line)
            if m and port is not None:
                local, host, busid = m.groups()
                att.append({"port": port, "local": local, "host": host or "", "busid": busid or "",
                            "vidpid": vidpid, "desc": desc})
        if att:
            return att
        # Sysfs gives us the local ports and busids for display and explicit detach.
        for path in sorted(glob.glob("/sys/devices/platform/vhci_hcd*/status*")):
            try:
                with open(path) as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            for ln in lines[1:]:
                p = ln.split()
                if len(p) < 3:
                    continue
                local = p[-1]
                if local == "0-0" or not re.match(r"\d+-[\d.]+$", local):
                    continue
                pnum = p[1] if p[0] in ("hs", "ss") else p[0]
                try:
                    pnum = str(int(pnum))
                except ValueError:
                    pass
                att.append({"port": pnum, "local": local, "host": "", "busid": "",
                            "vidpid": self._vidpid_of_local(local), "desc": ""})
        return att

    def attach(self, host, busid, quiet=False):
        if not self.module_loaded():
            self.load_module(quiet=quiet)
        return run(self.sudo() + [USBIP, "attach", "-r", host, "-b", busid], quiet=quiet)

    def detach(self, port):
        return run(self.sudo() + [USBIP, "detach", "-p", str(port)])

    @staticmethod
    def default_route():
        _, out, _ = run(["ip", "-4", "route", "show", "default"], timeout=5, quiet=True)
        m = re.search(r"default\s+via\s+(\d+\.\d+\.\d+\.\d+)\s+dev\s+(\S+)", out)
        return (m.group(1), m.group(2)) if m else None


# --------------------------------------------------------------------------
# auto-attach worker (Linux only)
# --------------------------------------------------------------------------
class Watcher(threading.Thread):
    """For each watched device: if the host exports it AND it is not attached
    locally -> attach it.  Otherwise do nothing, quietly.

      * device attached          -> silent, status "attached"
      * device not exported yet  -> silent, status "waiting" (board off/booting)
      * exported & not attached   -> one `usbip attach`, then back off on failure
      * host says "busy"          -> stale attachment on Windows; ONE hint, wait

    When every target is attached, only the local ports are checked.
    Event.wait() blocks the thread; it wakes on add/remove/stop.
    """

    def __init__(self, backend, log_fn):
        super().__init__(daemon=True)
        self.backend = backend
        self.log = log_fn
        self.interval = POLL_SEC
        self.base_route = None
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.targets = {}     # key -> {host, busid, vidpid, fails, next, ok_at, busy}
        self.status = {}      # key -> str
        self._logged = {}     # key -> last logged str

    @staticmethod
    def key(host, busid):
        address, separator, zone = host.partition("%")
        return "%s%s%s/%s" % (address.lower(), separator, zone, busid)

    @staticmethod
    def device_key(device):
        """Identify an export by its host and remote bus ID."""
        if device.get("host") and device["busid"]:
            return Watcher.key(device["host"], device["busid"])
        return None

    def watch(self, host, busid, vidpid=""):
        k = self.key(host, busid)
        with self._lock:
            self.targets[k] = {"host": host, "busid": busid, "vidpid": vidpid.lower(),
                               "fails": 0, "next": 0.0, "ok_at": 0.0, "busy": False}
            self.status[k] = "checking"
        self.log("[watch] + %s" % k)
        self._wake.set()

    def unwatch(self, k):
        with self._lock:
            gone = self.targets.pop(k, None)
            self.status.pop(k, None)
            self._logged.pop(k, None)
        if gone:
            self.log("[watch] - %s" % k)
        self._wake.set()

    def is_watched(self, host, busid):
        with self._lock:
            return self.key(host, busid) in self.targets

    def snapshot(self):
        with self._lock:
            return [{"key": k, "busid": t["busid"], "status": self.status.get(k, "?")}
                    for k, t in sorted(self.targets.items())]

    def stop(self):
        self._stop_evt.set()
        self._wake.set()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                self.log("[watch] tick error: %s" % e)
            self._wake.wait(max(1.0, self.interval))
            self._wake.clear()

    def _status(self, k, msg):
        with self._lock:
            if k not in self.targets:
                return
            self.status[k] = msg
        if self._logged.get(k) != msg:
            self._logged[k] = msg
            self.log("[watch] %s: %s" % (k, msg))

    def _tick(self):
        with self._lock:
            targets = list(self.targets.items())
        if not targets:
            return
        if self.base_route is None:
            self.base_route = self.backend.default_route()

        now = time.monotonic()
        att = self.backend.list_attached()
        attached = {}
        unknown_vidpids = set()
        for device in att:
            key = self.device_key(device)
            if key is None:
                unknown_vidpids.add(device["vidpid"])
            else:
                attached[key] = device["vidpid"]

        pending = []
        for k, t in targets:
            if k in attached and attached[k] and t["vidpid"] and attached[k] != t["vidpid"]:
                self._status(k, "waiting -- a different device is attached at this bus ID")
            elif k in attached or (t["ok_at"] and now - t["ok_at"] < GRACE_SEC):
                t["fails"] = 0
                t["busy"] = False
                self._status(k, "attached")
            elif unknown_vidpids and (not t["vidpid"] or "" in unknown_vidpids
                                      or t["vidpid"] in unknown_vidpids):
                self._status(k, "waiting -- can't verify the source of an imported device")
            else:
                pending.append((k, t))
        if not pending:
            return

        exported = {}
        for host in {t["host"] for _, t in pending}:
            try:
                exported[host] = {d["busid"]: d["vidpid"] for d in
                                  self.backend.list_remote(host, timeout=LIST_TIMEOUT)}
            except Exception:
                exported[host] = None

        for k, t in pending:
            exp = exported.get(t["host"])
            if exp is None:
                self._status(k, "waiting -- host %s not answering" % t["host"])
                continue
            if t["busid"] not in exp:
                self._status(k, "waiting -- device not present (board off / rebooting)")
                continue
            if t["vidpid"] and exp[t["busid"]] != t["vidpid"]:
                self._status(k, "waiting -- the host exports a different device at this bus ID")
                continue
            if now < t["next"]:
                continue
            loud = t["fails"] == 0
            rc, out, err = self.backend.attach(t["host"], t["busid"], quiet=not loud)
            msg = last_line(err, out) or ("rc=%d" % rc)
            if rc == 0:
                t["fails"], t["next"], t["ok_at"], t["busy"] = 0, 0.0, time.monotonic(), False
                self._status(k, "attached")
                self._check_route()
            elif "busy" in msg.lower():
                t["fails"] += 1
                t["busy"] = True
                t["next"] = now + BUSY_BACKOFF
                self._status(k, "stale attachment on the Windows host -- there run "
                                "`usbipd detach --busid %s` (or unplug/replug). waiting"
                                % t["busid"])
            else:
                t["fails"] += 1
                t["next"] = now + MISSING_BACKOFF
                self._status(k, "attach failed: %s -- retrying" % msg)

    def _check_route(self):
        r = self.backend.default_route()
        if r and self.base_route and r != self.base_route:
            self.log("! default route changed %s -> %s after attach." % (self.base_route, r))
            self.log("! A USB network gadget (BeagleBone usb0) likely grabbed routing. "
                     "Detach it, or use Help / Setup -> 'Shield routing'.")
            self.base_route = r


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
HELP_TEXT = """USB/IP GUI -- setup & notes

This app runs only:  usbip, usbipd, modprobe, ip route.
It does NOT change the Windows firewall, registry, services, or any file --
except the buttons at the bottom of this window, which say what they do.

======================================================================
WINDOWS HOST  (usbipd-win)
======================================================================
    winget install --interactive --exact dorssel.usbipd-win
    usbipd --version

Run this app (or usbipd) from an *Administrator* terminal.
"Share" = `usbipd bind` and is PERSISTENT: after the board reboots the device
re-shares itself, so only the Linux side must re-attach.

"Device busy (exported)" on the Linux side means Windows still thinks a
(dead) client holds the device -- from an earlier crash / lost network.
Clear it on Windows:
    usbipd detach --busid <BUSID>          (or:  usbipd detach --all )
    usbipd list        # STATE should read "Shared", not "Attached"
or just unplug and replug the device.  The Windows GUI has a "Force-detach"
button that does `usbipd detach --busid`.

======================================================================
UBUNTU  (Hyper-V VM)  (usbip)
======================================================================
    sudo apt update
    sudo apt install linux-tools-common linux-tools-$(uname -r)

Run the app as your normal user. For passwordless device operations with
sudo 1.9.10 or newer, open the policy with:

    sudo visudo -f /etc/sudoers.d/usbip

Replace any older rule in that file with:

    YOUR_USER ALL=(root) NOPASSWD: /usr/bin/usbip port, \\
        /usr/bin/usbip ^attach -r [a-zA-Z0-9._:%-]+ -b [0-9]+-[0-9]+([.][0-9]+)*$, \\
        /usr/bin/usbip ^detach -p [0-9]+$, /usr/sbin/modprobe vhci-hcd

Use your login name and the system binary paths shown in the startup log.
The app reports errors from usbip port at startup. On older sudo versions,
use literal attach -r HOST -b BUSID and detach -p PORT entries for the host,
bus IDs, and local ports you use. For interactive desktop authorization,
set the "sudo" field to pkexec.

Kernel module: loaded at startup and before each attach.  Load at boot:
    echo vhci-hcd | sudo tee /etc/modules-load.d/vhci-hcd.conf
    (or the button below)

======================================================================
WINDOWS HOST IP
======================================================================
On a Hyper-V "Default Switch" the host == the VM's default gateway, which
CHANGES on every host reboot.  The app auto-detects it (`ip route`); press
"Detect" after a host reboot, or type it in the Host field.
On an "External" switch, use the host's LAN IP.

======================================================================
LOCAL vs REMOTE BUSID  (why "Device busy (exported)" used to appear)
======================================================================
`usbip port` shows BOTH ids:

    Port 00: ...
        1-1 -> usbip://172.26.0.1:3240/10-3
        ^^^ local vhci id            ^^^^ remote busid (what you attach by)

The app matches attachments by exporting host and remote busid. When Linux
only reports the local port, the app shows a separate "source unknown" row
and pauses auto-attach for any device that might already occupy that port.
Select the local row to detach that port.

A row still marked "(attached, not exported)" is a real leftover: imported
locally while the host no longer offers that busid.  Select it and press
Detach, or "Detach all"  (or:  sudo usbip detach -p <PORT> ).

======================================================================
DEVICE TYPES -- WATCH OUT FOR NETWORK ADAPTERS
======================================================================
Only attach what you actually need (usually a USB-serial console, e.g.
Prolific 067b:2303 / FTDI / CP210x, or the board's composite gadget
1d6b:0104).

A USB *Ethernet* adapter (Realtek 0bda:8153, ASIX ax88xxx, RNDIS, ...) is
almost never what you want in the VM: attaching it hands the interface to
NetworkManager, which makes it the default route and drops your session.
The app warns before attaching anything that looks like a NIC.

======================================================================
"BeagleBone / network gadget hijacks my connection"
======================================================================
The BBB USB gadget also exposes a network interface (usb0).  When attached,
NetworkManager tries to activate it and can make it the default route --
which drops your SSH / X session (looks like "Hyper-V closed and reopened";
the VM itself keeps running).

Fix (opt-in button below): writes a NetworkManager drop-in so USB-Ethernet
gadgets are left unmanaged and can never become the default route:
    /etc/NetworkManager/conf.d/99-usbip-gui-gadgets.conf
        [device-usbip-gui]
        match-device=driver:cdc_ether,driver:rndis_host,driver:cdc_ncm,
                     driver:cdc_subset,driver:cdc_eem,driver:r8152, ...
        managed=0
"Undo" deletes that file.  You can still bring usb0 up by hand if you need
to reach the board over USB-network.

Note: the "at boot" / "Shield" / "Undo" buttons run `sh` through sudo, which
the usbip-only sudoers rule above does not allow.  If they fail, run the
commands shown here in a terminal yourself.

======================================================================
FIREWALL  (if usbip list times out)
======================================================================
Shared devices are available to clients that can reach TCP 3240. Scope the
existing firewall rule to the Linux VM IP or trusted VM subnet. In Windows
Administrator PowerShell:

    $vmAddress = Read-Host 'Enter the Linux VM IP address or trusted VM subnet (CIDR)'
    Set-NetFirewallRule -DisplayName 'usbipd' -RemoteAddress $vmAddress -Enabled True

If the rule is missing, repair the usbipd-win installation and apply the
scope to the restored rule.

======================================================================
COMMANDS THE APP RUNS
======================================================================
Windows:  usbipd state|list ,  usbipd bind --busid ,
          usbipd unbind --busid|--all ,  usbipd detach --busid|--all
Linux  :  usbip list -r <host> ,  usbip port ,
          usbip attach -r <host> -b <busid> ,  usbip detach -p <port> ,
          modprobe vhci-hcd ,  ip route
          (without root, attachment state falls back to
           /sys/devices/platform/vhci_hcd*/status)
"""

NM_CONF = "/etc/NetworkManager/conf.d/99-usbip-gui-gadgets.conf"
NM_BODY = ("[device-usbip-gui]\n"
           "match-device=driver:cdc_ether,driver:rndis_host,driver:cdc_ncm,"
           "driver:cdc_subset,driver:cdc_eem,driver:r8152,driver:r8153_ecm,"
           "driver:ax88179_178a,driver:asix,driver:aqc111\n"
           "managed=0\n")

# heuristics: does an exported device look like a USB network adapter?
_NIC_RE = re.compile(r"ethernet|network adapter|\brndis\b|gigabit|\blan\b|\bnic\b|"
                     r"rtl81\d\d|815[23]|ax88\d{3}", re.I)
_NIC_IDS = {"0bda:8153", "0bda:8152", "0b95:1790", "0b95:178a", "0b95:1720",
            "17e8:2011", "2357:0601", "0951:1790"}


def looks_like_nic(d):
    return bool(_NIC_RE.search(d.get("desc", "")) or d.get("vidpid", "").lower() in _NIC_IDS)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        set_logger(self.q.put)
        self.win = IS_WINDOWS

        self.sudo_var = tk.StringVar(value=DEFAULT_SUDO)
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        self.interval_var = tk.StringVar(value=str(POLL_SEC))
        self.autorefresh_var = tk.BooleanVar(value=True)
        self.rows = {}
        self._listing = False
        self._wsig = None
        self._last_msg, self._last_n = None, 0

        if not self.win:
            global USBIP, MODPROBE
            USBIP = resolve_usbip()
            MODPROBE = shutil.which("modprobe") or "/usr/sbin/modprobe"

        self.backend = WindowsBackend() if self.win else LinuxBackend(DEFAULT_SUDO)
        self.watcher = None
        if not self.win:
            self.watcher = Watcher(self.backend, self.log)
            self.watcher.start()

        root.title("usbip GUI - " + ("Windows: share" if self.win else "Linux: client"))
        root.minsize(800, 580)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._pump()

        if self.win:
            self.submit(lambda: self._check(USBIPD, "--version",
                        "winget install --interactive --exact dorssel.usbipd-win"))
            self.refresh()
        else:
            self.log("[setup] usbip binary: %s" % USBIP)
            self.log("[setup] modprobe binary: %s" % MODPROBE)
            self.submit(lambda: self._check(USBIP, "version",
                        "sudo apt install linux-tools-common linux-tools-$(uname -r)"))
            self.submit(self._startup_linux, name="startup")
            self._detect(force=False)          # refreshes once the host is known
        self.root.after(AUTOREFRESH_SEC * 1000, self._tick_autorefresh)

    # ---- layout ------------------------------------------------------
    def _build(self):
        pad = dict(padx=6, pady=4)
        top = ttk.Frame(self.root); top.pack(fill="x", **pad)
        if not self.win:
            ttk.Label(top, text="Host:").pack(side="left")
            ttk.Entry(top, textvariable=self.host_var, width=15).pack(side="left", padx=2)
            ttk.Button(top, text="Detect", width=7,
                       command=lambda: self._detect(force=True)).pack(side="left", padx=(0, 10))
            ttk.Label(top, text="sudo:").pack(side="left")
            ttk.Entry(top, textvariable=self.sudo_var, width=9).pack(side="left", padx=(2, 10))
            ttk.Label(top, text="poll").pack(side="left")
            ttk.Spinbox(top, from_=1, to=10, increment=0.5, width=4,
                        textvariable=self.interval_var,
                        command=self._apply_interval).pack(side="left", padx=2)
            ttk.Label(top, text="s").pack(side="left", padx=(0, 10))
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Checkbutton(top, text="auto", variable=self.autorefresh_var).pack(side="left", padx=4)
        ttk.Button(top, text="Help / Setup", command=self._help).pack(side="right")

        if self.win:
            cols, widths = ("busid", "vidpid", "desc", "state"), (58, 88, 380, 150)
        else:
            cols, widths = ("busid", "vidpid", "desc", "port", "watch"), (58, 88, 330, 50, 55)
        mid = ttk.Frame(self.root); mid.pack(fill="both", expand=True, padx=6)
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=9)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c.upper())
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._primary_action())

        bar = ttk.Frame(self.root); bar.pack(fill="x", **pad)
        if self.win:
            ttk.Button(bar, text="Share", command=lambda: self._bind(False)).pack(side="left")
            ttk.Button(bar, text="Share (force)", command=lambda: self._bind(True)).pack(side="left", padx=4)
            ttk.Button(bar, text="Unshare", command=self._unbind).pack(side="left")
            ttk.Button(bar, text="Unshare all", command=self._unbind_all).pack(side="left", padx=4)
            ttk.Button(bar, text="Force-detach", command=self._wdetach).pack(side="left")
            ttk.Button(bar, text="Force-detach all", command=self._wdetach_all).pack(side="left", padx=4)
        else:
            ttk.Button(bar, text="Attach", command=self._attach_once).pack(side="left")
            ttk.Button(bar, text="Keep attached", command=self._watch).pack(side="left", padx=4)
            ttk.Button(bar, text="Stop", command=self._unwatch).pack(side="left")
            ttk.Button(bar, text="Detach", command=self._detach).pack(side="left", padx=4)
            ttk.Button(bar, text="Detach all", command=self._detach_all).pack(side="left")

        if not self.win:
            ttk.Label(self.root, text="Kept attached").pack(anchor="w", padx=8)
            wf = ttk.Frame(self.root); wf.pack(fill="x", padx=6)
            self.wtree = ttk.Treeview(wf, columns=("key", "status"), show="headings", height=4)
            self.wtree.heading("key", text="HOST / BUSID")
            self.wtree.heading("status", text="STATUS")
            self.wtree.column("key", width=230); self.wtree.column("status", width=470)
            self.wtree.pack(fill="x")

        lh = ttk.Frame(self.root); lh.pack(fill="x", padx=8)
        ttk.Label(lh, text="Log").pack(side="left")
        ttk.Button(lh, text="clear", command=self._clear_log).pack(side="right")
        lf = ttk.Frame(self.root); lf.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.logbox = tk.Text(lf, height=9, wrap="none", state="disabled")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

    # ---- log -------------------------------------------------------
    def log(self, msg):
        self.q.put(str(msg))

    def _clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")
        self._last_msg, self._last_n = None, 0

    def _emit(self, msg):
        box = self.logbox
        box.configure(state="normal")
        if msg == self._last_msg and box.index("end-1c") != "1.0":
            self._last_n += 1
            box.delete("end-1l", "end")
            box.insert("end", "%s  (x%d)\n" % (msg, self._last_n))
        else:
            self._last_msg, self._last_n = msg, 1
            box.insert("end", msg + "\n")
        if int(box.index("end-1c").split(".")[0]) > 1000:
            box.delete("1.0", "end-600l")
        box.see("end")
        box.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if callable(item):
                    item()
                else:
                    self._emit(item)
        except queue.Empty:
            pass
        if self.watcher:
            self._refresh_watch_tree()
        self.root.after(200, self._pump)

    # ---- workers -------------------------------------------------
    def submit(self, fn, on_done=None, name="op"):
        def worker():
            try:
                res, ok = fn(), True
            except Exception as e:  # noqa: BLE001
                res, ok = e, False
            self.q.put(lambda: self._done(name, ok, res, on_done))
        threading.Thread(target=worker, daemon=True, name=name).start()

    def _done(self, name, ok, res, on_done):
        if not ok:
            self.log("[%s] error: %s" % (name, res))
        if on_done:
            on_done(res if ok else None)

    def _startup_linux(self):
        self.backend.load_module()
        if self.watcher.base_route is None:
            self.watcher.base_route = LinuxBackend.default_route()
        rc, out, err = self.backend.read_ports()
        att = self.backend.list_attached(out)
        src = "usbip port" if any(a["busid"] for a in att) else "vhci sysfs"
        self.log("[setup] attached now (%s): %s" % (src, ", ".join(
            "%s%s on port %s" % (a["busid"] or a.get("local", "?"),
                                 " " + a["vidpid"] if a["vidpid"] else "", a["port"])
            for a in att) or "none"))
        if rc != 0:
            self.log("! usbip port failed: %s" % (last_line(err, out) or "rc=%d" % rc))
            self.log("! See Help / Setup for Linux permissions and kernel module setup.")

    def _apply_interval(self):
        try:
            self.watcher.interval = max(1.0, float(self.interval_var.get()))
        except (ValueError, AttributeError):
            pass

    def _check(self, tool, verarg, hint):
        rc, _, _ = run([tool, verarg], timeout=5, quiet=True)
        if rc == 127:
            self.log("[setup] '%s' not found -> %s" % (tool, hint))

    # ---- selection ---------------------------------------------
    def _selected(self):
        sel = self.tree.selection()
        return self.rows.get(sel[0]) if sel else None

    def _need_selection(self, remote=False):
        d = self._selected()
        if not d:
            messagebox.showinfo("usbip GUI", "Select a device in the list first.")
        elif remote and "local" in d:
            messagebox.showinfo("usbip GUI", "Select a device exported by the Windows host.")
            return None
        return d

    # ---- host detect -----------------------------------------
    def _detect(self, force):
        def done(ip):
            cur = self.host_var.get().strip()
            if ip and (force or not cur):
                self.host_var.set(ip)
                self.log("[detect] Windows host = %s" % ip)
            elif ip and ip != cur:
                self.log("[detect] gateway is %s (keeping host %s)" % (ip, cur))
            elif not ip:
                self.log("[detect] host IP not found -- type it in the Host field")
            self.refresh()
        self.submit(detect_windows_host, done, name="detect")

    # ---- refresh --------------------------------------------
    def refresh(self):
        if self._listing:
            return
        self._listing = True
        if self.win:
            self.submit(self.backend.list_devices,
                        self._after_list(self._fill_windows), name="list")
        else:
            self.backend.sudo_str = self.sudo_var.get()
            self._apply_interval()
            host = self.host_var.get().strip()
            self.submit(lambda: self._collect_linux(host),
                        self._after_list(self._fill_linux), name="list")

    def _after_list(self, filler):
        def cb(res):
            self._listing = False
            filler(res)
        return cb

    def _collect_linux(self, host):
        out = {"host": host, "remote": [], "attached": [], "err": None}
        try:
            out["remote"] = self.backend.list_remote(host, timeout=LIST_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            out["err"] = str(e)
        try:
            out["attached"] = self.backend.list_attached()
        except Exception:
            pass
        return out

    @staticmethod
    def _selection_key(d):
        return Watcher.device_key(d) or (d.get("port"), d.get("local") or d["busid"])

    def _remember(self):
        d = self._selected()
        return self._selection_key(d) if d else None

    def _restore(self, key):
        for iid, d in self.rows.items():
            if self._selection_key(d) == key:
                self.tree.selection_set(iid)
                return

    def _fill_windows(self, devs):
        if devs is None:
            return
        keep = self._remember()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for d in devs:
            iid = self.tree.insert("", "end",
                                   values=(d["busid"], d["vidpid"], d["desc"], d["state"]))
            self.rows[iid] = d
        if keep:
            self._restore(keep)

    def _fill_linux(self, data):
        if data is None:
            return
        if data["err"]:
            self.log("[list] %s" % data["err"])
        keep = self._remember()
        att = data["attached"]
        ports = {}
        for a in att:
            key = Watcher.device_key(a)
            if key is not None:
                ports[key] = a["port"]
        host = data["host"]
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        shown = set()
        for device in data["remote"]:
            d = {**device, "host": host}
            port = ports.get(Watcher.device_key(d))
            d["port"] = port
            if port is not None:
                shown.add(port)
            watch = "yes" if self.watcher.is_watched(host, d["busid"]) else "-"
            iid = self.tree.insert("", "end",
                                   values=(d["busid"], d["vidpid"], d["desc"], port or "-", watch))
            self.rows[iid] = d
        for a in att:
            if a["port"] in shown:
                continue
            if not a["host"]:
                status = " (attached, source unknown)"
            elif Watcher.key(a["host"], "") != Watcher.key(host, ""):
                status = " (attached from %s)" % a["host"]
            else:
                status = " (attached, not exported)"
            iid = self.tree.insert("", "end",
                                   values=(a["busid"] or a["local"], a["vidpid"],
                                           a["desc"] + status, a["port"], "-"))
            self.rows[iid] = a
        if keep:
            self._restore(keep)

    def _refresh_watch_tree(self):
        snap = self.watcher.snapshot()
        sig = tuple((r["key"], r["status"]) for r in snap)
        if sig == self._wsig:
            return
        self._wsig = sig
        keys = {r["key"] for r in snap}
        sel = [k for k in self.wtree.selection() if k in keys]
        self.wtree.delete(*self.wtree.get_children())
        for r in snap:
            self.wtree.insert("", "end", iid=r["key"], values=(r["key"], r["status"]))
        if sel:
            self.wtree.selection_set(sel)

    # ---- Windows actions ------------------------------------
    def _bind(self, force):
        d = self._need_selection()
        if d:
            self.submit(lambda: self.backend.bind(d["busid"], force),
                        lambda _r: self.refresh(), name="bind")

    def _unbind(self):
        d = self._need_selection()
        if d:
            self.submit(lambda: self.backend.unbind(d["busid"]),
                        lambda _r: self.refresh(), name="unbind")

    def _unbind_all(self):
        self.submit(self.backend.unbind_all, lambda _r: self.refresh(), name="unbind")

    def _wdetach(self):
        d = self._need_selection()
        if d:
            self.submit(lambda: self.backend.detach(d["busid"]),
                        lambda _r: self.refresh(), name="detach")

    def _wdetach_all(self):
        if messagebox.askokcancel("Force-detach all",
                                  "Disconnect every attached client from this host?"):
            self.submit(self.backend.detach_all, lambda _r: self.refresh(), name="detach")

    # ---- Linux actions ------------------------------------
    def _confirm(self, d):
        if not CONFIRM_ATTACH:
            return True
        if d["port"] is not None:
            return True
        if looks_like_nic(d):
            return messagebox.askokcancel(
                "This looks like a NETWORK ADAPTER",
                "%s  %s  (%s) from %s\n\n"
                "Attaching a USB network adapter almost always breaks this VM's\n"
                "routing and drops your SSH / X session (and if the Windows host\n"
                "uses it for internet, the host loses network too).\n\n"
                "Only continue if you have run Help / Setup -> 'Shield routing'\n"
                "and will set the interface address by hand.\n\nAttach anyway?"
                % (d.get("desc", ""), d["busid"], d.get("vidpid", "?"), d["host"]),
                icon="warning", default="cancel")
        return messagebox.askokcancel(
            "Attach to this VM?",
            "Attach  %s  (%s) from %s\n%s\n\n"
            "The Windows host loses this device until you Detach."
            % (d["busid"], d.get("vidpid", "?"), d["host"], d.get("desc", "")))

    def _attach_once(self):
        d = self._need_selection(remote=True)
        if not d or not self._confirm(d):
            return
        self.submit(lambda: self.backend.attach(d["host"], d["busid"]),
                    lambda _r: self.refresh(), name="attach")

    def _watch(self):
        d = self._need_selection(remote=True)
        if not d or not self._confirm(d):
            return
        self.watcher.watch(d["host"], d["busid"], d["vidpid"])
        self.refresh()

    def _unwatch(self):
        sel = list(self.wtree.selection())
        if not sel:
            key = Watcher.device_key(self._selected() or {})
            if key is not None:
                sel = [key]
        if not sel:
            messagebox.showinfo("usbip GUI", "Select a kept-attached row (or a device).")
            return
        for k in sel:
            self.watcher.unwatch(k)
        self.refresh()

    def _detach(self):
        d = self._selected()
        key = Watcher.device_key(d or {})
        if d is None:
            sel = self.wtree.selection()
            if not sel:
                messagebox.showinfo("usbip GUI", "Select an attached device to detach.")
                return
            key = sel[0]
        if key is not None:
            self.watcher.unwatch(key)
        target = self._selection_key(d) if d is not None else key
        label = key or "port " + d["port"]

        def work():
            for a in self.backend.list_attached():
                if self._selection_key(a) == target:
                    return self.backend.detach(a["port"])
            self.log("[detach] %s is no longer attached" % label)

        self.submit(work, lambda _r: self.refresh(), name="detach")

    def _detach_all(self):
        if not messagebox.askokcancel("Detach all",
                                      "Detach every imported device and stop keeping "
                                      "any attached?"):
            return
        for r in self.watcher.snapshot():
            self.watcher.unwatch(r["key"])

        def work():
            for a in self.backend.list_attached():
                self.backend.detach(a["port"])
            return True
        self.submit(work, lambda _r: self.refresh(), name="detach-all")

    def _primary_action(self):
        if self.win:
            d = self._selected()
            if d:
                self._unbind() if "Shared" in d.get("state", "") else self._bind(False)
        else:
            self._watch()

    # ---- help --------------------------------------------
    def _help(self):
        w = tk.Toplevel(self.root)
        w.title("USB/IP GUI - Help / Setup")
        w.minsize(760, 600)
        f = ttk.Frame(w); f.pack(fill="both", expand=True, padx=8, pady=8)
        t = tk.Text(f, wrap="word")
        s = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=s.set)
        t.insert("1.0", HELP_TEXT)
        t.configure(state="disabled")
        t.pack(side="left", fill="both", expand=True)
        s.pack(side="left", fill="y")
        if not self.win:
            b = ttk.Frame(w); b.pack(fill="x", padx=8, pady=(0, 8))
            ttk.Button(b, text="Load vhci-hcd now",
                       command=lambda: self.submit(self.backend.load_module,
                                                   name="modprobe")).pack(side="left")
            ttk.Button(b, text="Load vhci-hcd at boot",
                       command=self._enable_boot_module).pack(side="left", padx=6)
            ttk.Button(b, text="Shield routing from USB gadgets",
                       command=self._shield_routing).pack(side="left")
            ttk.Button(b, text="Undo shield",
                       command=self._unshield_routing).pack(side="left", padx=6)
        ttk.Button(w, text="Close", command=w.destroy).pack(pady=(0, 8))

    def _enable_boot_module(self):
        self.submit(lambda: run(self.backend.sudo() +
                    ["sh", "-c", "echo vhci-hcd > /etc/modules-load.d/vhci-hcd.conf"]),
                    name="boot-module")

    def _shield_routing(self):
        if not messagebox.askokcancel(
                "Shield routing",
                "Write %s so USB-Ethernet gadgets stay unmanaged and can never\n"
                "become the default route, then reload NetworkManager?" % NM_CONF):
            return
        script = ("printf '%s' > %s && (nmcli general reload 2>/dev/null || "
                  "systemctl reload NetworkManager)" % (NM_BODY, shlex.quote(NM_CONF)))
        self.submit(lambda: run(self.backend.sudo() + ["sh", "-c", script]),
                    name="shield")

    def _unshield_routing(self):
        script = ("rm -f %s && (nmcli general reload 2>/dev/null || "
                  "systemctl reload NetworkManager)" % shlex.quote(NM_CONF))
        self.submit(lambda: run(self.backend.sudo() + ["sh", "-c", script]),
                    name="unshield")

    # ---- misc -------------------------------------------
    def _tick_autorefresh(self):
        if self.autorefresh_var.get():
            self.refresh()
        self.root.after(AUTOREFRESH_SEC * 1000, self._tick_autorefresh)

    def _on_close(self):
        if self.watcher:
            self.watcher.stop()
        self.root.destroy()


def main():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print("Cannot open a window:", e)
        print("If you started this with sudo over SSH, that breaks X11 auth.")
        print("Run it as your normal user:   python3 usbip_gui.py")
        print("and give usbip passwordless sudo (see README / Help).")
        raise SystemExit(1)
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
