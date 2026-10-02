"""Start Birdsong automatically.

  Linux    a systemd *user* service (starts at login; with `loginctl
           enable-linger` it starts at boot without anyone logging in)
  Windows  a hidden launcher in your Startup folder (starts when you log in)
  macOS    a LaunchAgent (starts when you log in)
"""

import os
import shutil
import subprocess
import sys

from .config import ROOT

NAME = "birdsong"


def _linux_unit_path():
    return os.path.expanduser("~/.config/systemd/user/birdsong.service")


def _windows_startup_path():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", "Birdsong.vbs")


def _mac_plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/io.github.wyldveil.birdsong.plist")


def install():
    if sys.platform.startswith("linux"):
        if not shutil.which("systemctl"):
            sys.exit("systemd not found. Add `{}/run.sh run --no-browser` to your startup applications instead.".format(ROOT))
        path = _linux_unit_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(f"""[Unit]
Description=Birdsong bird sound identifier
After=network-online.target pipewire-pulse.service sound.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart=/bin/sh {ROOT}/run.sh run --no-browser
Restart=always
RestartSec=15
Nice=10

[Install]
WantedBy=default.target
""")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "birdsong.service"], check=False)
        print("Installed and started: systemctl --user status birdsong")
        print("To have it start at boot even when nobody is logged in, run once:")
        print(f"    loginctl enable-linger {os.environ.get('USER', '$USER')}")
        print("Logs: journalctl --user -u birdsong -f   (or data/birdsong.log)")
    elif sys.platform == "win32":
        path = _windows_startup_path()
        bat = os.path.join(ROOT, "run.bat")
        # Run hidden (window style 0); BIRDSONG_NOPAUSE stops run.bat waiting for a key on errors.
        cmd = f'cmd /c set BIRDSONG_NOPAUSE=1&& ""{bat}"" run --no-browser'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('Set sh = CreateObject("WScript.Shell")\r\n')
            fh.write(f'sh.Run "{cmd}", 0, False\r\n')
        print(f"Installed: Birdsong will start (hidden) when you log in.\n  {path}")
        print("Start it now without logging out by double-clicking that file, or run run.bat.")
        print("Logs: data\\birdsong.log")
    elif sys.platform == "darwin":
        path = _mac_plist_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.github.wyldveil.birdsong</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>{ROOT}/run.sh</string><string>run</string><string>--no-browser</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{ROOT}/data/launchd.log</string>
  <key>StandardErrorPath</key><string>{ROOT}/data/launchd.log</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", path], capture_output=True)
        subprocess.run(["launchctl", "load", path], check=False)
        print(f"Installed: {path}\nmacOS will ask for microphone permission the first time.")
    else:
        sys.exit(f"Autostart isn't supported on {sys.platform}.")


def remove():
    if sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "birdsong.service"], check=False)
        _rm(_linux_unit_path())
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    elif sys.platform == "win32":
        _rm(_windows_startup_path())
        print("Removed. If Birdsong is running now, stop it from Task Manager (python.exe) or by rebooting.")
    elif sys.platform == "darwin":
        subprocess.run(["launchctl", "unload", _mac_plist_path()], capture_output=True)
        _rm(_mac_plist_path())
    print("Autostart removed.")


def status():
    path = {"win32": _windows_startup_path(), "darwin": _mac_plist_path()}.get(sys.platform, _linux_unit_path())
    print(f"Autostart {'installed' if os.path.exists(path) else 'not installed'} ({path})")
    if sys.platform.startswith("linux") and os.path.exists(path):
        subprocess.run(["systemctl", "--user", "--no-pager", "status", "birdsong.service"], check=False)


def _rm(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
