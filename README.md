# usbip-gui

A small, single-file Tkinter front-end for **USB/IP**: share a USB device from a
Windows host with [usbipd-win](https://github.com/dorssel/usbipd-win) and attach
it inside a Linux VM (e.g. Ubuntu on Hyper-V) with `usbip`, with an
**auto re-attach** mode that keeps a device connected while the board it belongs
to reboots.

It was built for embedded development with a BeagleBone Black (USB-serial
console + USB gadget) that gets reset often, but works with any USB device.

Author: **Yassine El Harami** · License: [MIT](LICENSE)

## Screenshots

**Linux client**: a USB-serial adapter kept attached across board reboots

![Linux client](docs/linux.png)

**Windows host**: sharing devices with usbipd-win

![Windows host](docs/windows.png)

## Features

Run **one copy per machine** — the app detects the OS and shows the right controls.

| Machine | What you can do |
|---|---|
| **Windows host** (usbipd-win) | List devices · Share / Share (force) / Unshare / Unshare all · Force-detach / Force-detach all |
| **Linux client** (usbip) | List exported devices · Attach · **Keep attached** · Stop · Detach · Detach all |

- **Keep attached**: re-attaches a device automatically as soon as the host
  exports it again (after a board reset or a cable re-plug), and stays quiet
  while nothing is wrong.
- **Host auto-detection**: in a Hyper-V "Default Switch" VM the Windows host is
  the default gateway; the app finds it at startup (and with **Detect**).
- **Network-adapter guard**: warns before attaching anything that looks like a
  USB Ethernet adapter, since that can steal the VM's default route.
- **Transparent log**: every command the app runs is printed with its result.
- **Help / Setup** window with the install and troubleshooting notes built in.

## Requirements

- Python 3.7+ with Tkinter (included with the python.org Windows installer;
  on Ubuntu: `sudo apt install python3-tk`). No third-party packages.
- **Windows host**: usbipd-win
  ```
  winget install --interactive --exact dorssel.usbipd-win
  ```
- **Linux client**: the `usbip` tool and the `vhci-hcd` kernel module
  ```
  sudo apt install linux-tools-common linux-tools-$(uname -r)
  ```
- TCP port **3240** reachable from the VM to the host (usbipd-win's installer
  normally adds the firewall rule).

## Run

No build step: it is one script.

```
python3 usbip_gui.py        # Linux
python  usbip_gui.py        # Windows, from an Administrator terminal
```

### Linux: don't run it with sudo

Run the app as your normal user (`sudo python3 usbip_gui.py` over SSH/X11
fails with `couldn't connect to display`) and give `usbip` passwordless sudo
instead:

```
echo "$USER ALL=(root) NOPASSWD: $(command -v usbip), $(command -v modprobe)" | sudo tee /etc/sudoers.d/usbip
sudo chmod 440 /etc/sudoers.d/usbip
```

The app checks this at startup and prints the exact command if it's missing.
The `sudo` field in the toolbar can be changed (e.g. to `pkexec` on a desktop
session).

### Optional: standalone Windows executable

```
pip install pyinstaller
pyinstaller --onefile --noconsole usbip_gui.py
```

The result is `dist\usbip_gui.exe`; run it as Administrator.

## Typical workflow

1. **Windows**: select the device → **Share**. Sharing (`usbipd bind`) is
   persistent, so it survives the device re-enumerating.
2. **Linux**: check the Host field (auto-detected) → select the device →
   **Keep attached** (or **Attach** for a one-off).
3. Reboot the board as often as you like; the Linux side re-attaches it within
   a poll or two.

## How "Keep attached" works

It never re-attaches blindly. Each poll (default 2 s) it:

1. reads what is actually imported (`usbip port`, which maps local vhci ports
   to remote busids; without root it falls back to vhci sysfs + vid:pid);
2. if the watched device is already attached → does nothing;
3. if the host isn't exporting it (board off / rebooting) → waits silently;
4. only if the host exports it **and** it isn't attached → runs one
   `usbip attach`; on failure it backs off and logs the reason once.

> **Local vs remote busid.** `usbip port` shows `1-1 -> usbip://<host>:3240/10-3`.
> The left id is the *local* vhci id (and is what sysfs calls `local_busid`);
> the right one is the remote busid you attach by. Comparing the two leads to
> needless re-attaches and `Device busy (exported)` errors, which this app avoids.

## Troubleshooting

**`Device busy (exported)`** — usbipd-win still thinks a (possibly dead) client
holds the device. On Windows press **Force-detach** (or run
`usbipd detach --busid <BUSID>` / `usbipd detach --all`), or unplug and replug it.

**Row marked "(attached, not exported)"** — imported locally while the host no
longer offers it. Select it and press **Detach** (or **Detach all**).

**SSH / X session drops after attaching a USB network adapter** —
NetworkManager made the new interface the default route. Use
**Help / Setup → Shield routing from USB gadgets**, which writes
`/etc/NetworkManager/conf.d/99-usbip-gui-gadgets.conf` so USB-Ethernet
interfaces stay unmanaged (**Undo shield** removes it).

**`usbip list` times out** — allow inbound TCP 3240 on the Windows host:
```
New-NetFirewallRule -DisplayName usbipd -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3240
```

## What the app touches

It only runs `usbipd`, `usbip`, `modprobe` and `ip route`. It does not change the
firewall, registry, services or system files, except through three opt-in
buttons in the Help window, each of which says what it writes:
*Load vhci-hcd at boot*, *Shield routing* and *Undo shield*. These run `sh`
through sudo; if your sudoers rule only allows `usbip`/`modprobe`, run the
equivalent commands from the Help text yourself.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Yassine El Harami.
