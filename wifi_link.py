"""OS Wi-Fi helpers for joining the microscope AP (Windows-first)."""

from __future__ import annotations

import platform
import re
import socket
import subprocess
import tempfile
import time
import xml.sax.saxutils
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _run(args: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


@dataclass
class WifiInterface:
    name: str
    state: str = ""
    ssid: str = ""
    signal: str = ""
    description: str = ""
    ipv4: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ipv4"] = self.ipv4 or []
        return d


def system() -> str:
    return platform.system().lower()


def list_interfaces() -> list[WifiInterface]:
    if system() == "windows":
        return _win_interfaces()
    if system() == "darwin":
        return _mac_interfaces()
    return _linux_interfaces()


def scan_networks(interface: str | None = None) -> list[dict[str, str]]:
    if system() == "windows":
        return _win_scan(interface)
    if system() == "darwin":
        return _mac_scan()
    return _linux_scan(interface)


def connect(
    ssid: str,
    password: str = "12345678",
    interface: str | None = None,
) -> dict[str, Any]:
    if system() == "windows":
        return _win_connect(ssid, password, interface)
    if system() == "darwin":
        return _mac_connect(ssid, password, interface)
    return _linux_connect(ssid, password, interface)


def disconnect(interface: str | None = None) -> dict[str, Any]:
    if system() == "windows":
        return _win_disconnect(interface)
    if system() == "darwin":
        return {"ok": False, "error": "macOS disconnect: use networksetup / System Settings"}
    return _linux_disconnect(interface)


def ensure_host_route(scope_ip: str, interface: str | None = None) -> dict[str, Any]:
    """Pin scope_ip via the chosen Wi-Fi interface (helps when LAN shares 192.168.1.0/24)."""
    if system() == "windows":
        return _win_ensure_route(scope_ip, interface)
    return {
        "ok": False,
        "error": f"host-route helper not implemented for {system()}; add a route manually to {scope_ip}",
    }


def ping_host(ip: str, timeout_s: float = 1.5) -> dict[str, Any]:
    # Prefer TCP connect to scope HTTP — ICMP on Windows can "succeed" with
    # "Destination host unreachable" from the wrong interface.
    try:
        sock = socket.create_connection((ip, 80), timeout=timeout_s)
        sock.close()
        return {"ok": True, "reachable": True, "method": "tcp/80"}
    except OSError as e_tcp:
        tcp_err = str(e_tcp)
    try:
        sock = socket.create_connection((ip, 8192), timeout=timeout_s)
        sock.close()
        return {"ok": True, "reachable": True, "method": "tcp/8192"}
    except OSError:
        pass
    return {
        "ok": False,
        "reachable": False,
        "method": "tcp",
        "error": tcp_err,
    }


def status(scope_ip: str = "192.168.1.254", preferred_iface: str | None = None) -> dict[str, Any]:
    ifaces = list_interfaces()
    andon = [i for i in ifaces if i.ssid.lower().startswith("andonstar")]
    chosen = None
    if preferred_iface:
        chosen = next((i for i in ifaces if i.name == preferred_iface), None)
    if chosen is None and andon:
        chosen = andon[0]
    # Prefer a non-primary adapter currently on Andonstar, else first disconnected USB-ish name
    reach = ping_host(scope_ip)
    return {
        "platform": system(),
        "interfaces": [i.to_dict() for i in ifaces],
        "andonstar_links": [i.to_dict() for i in andon],
        "preferred": preferred_iface,
        "active": chosen.to_dict() if chosen else None,
        "scope_ip": scope_ip,
        "scope_reachable": reach.get("reachable", False),
        "reach": reach,
    }


def find_andonstar_ssids(interface: str | None = None) -> list[dict[str, str]]:
    nets = scan_networks(interface)
    return [n for n in nets if n.get("ssid", "").lower().startswith("andonstar")]


# ---------- Windows ----------

def _win_interfaces() -> list[WifiInterface]:
    p = _run(["netsh", "wlan", "show", "interfaces"])
    text = p.stdout or ""
    blocks = re.split(r"\n(?=\s*Name\s+:)", text)
    out: list[WifiInterface] = []
    for block in blocks:
        name_m = re.search(r"^\s*Name\s+:\s*(.+)$", block, re.M)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        def g(pat: str) -> str:
            m = re.search(pat, block, re.M | re.I)
            return m.group(1).strip() if m else ""
        iface = WifiInterface(
            name=name,
            state=g(r"^\s*State\s+:\s*(.+)$"),
            ssid=g(r"^\s*SSID\s+:\s*(.+)$"),
            signal=g(r"^\s*Signal\s+:\s*(.+)$"),
            description=g(r"^\s*Description\s+:\s*(.+)$"),
            ipv4=_win_ipv4(name),
        )
        out.append(iface)
    return out


def _win_ipv4(interface: str) -> list[str]:
    # Prefer PowerShell Get-NetIPAddress for accuracy
    ps = (
        f"Get-NetIPAddress -InterfaceAlias '{interface.replace(chr(39), '')}' "
        f"-AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        f"Select-Object -ExpandProperty IPAddress"
    )
    p = _run(["powershell", "-NoProfile", "-Command", ps], timeout=10)
    ips = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    return ips


def _win_scan(interface: str | None = None) -> list[dict[str, str]]:
    # Refresh scan
    args = ["netsh", "wlan", "show", "networks", "mode=bssid"]
    p = _run(args, timeout=25)
    text = p.stdout or ""
    nets: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        m = re.match(r"\s*SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            if current and current.get("ssid") is not None:
                nets.append(current)
            ssid = m.group(1).strip()
            current = {"ssid": ssid, "signal": "", "auth": "", "radio": ""}
            continue
        if not current:
            continue
        sm = re.search(r"Signal\s*:\s*(.+)", line)
        if sm and not current["signal"]:
            current["signal"] = sm.group(1).strip()
        am = re.search(r"Authentication\s*:\s*(.+)", line)
        if am and not current["auth"]:
            current["auth"] = am.group(1).strip()
        rm = re.search(r"Radio type\s*:\s*(.+)", line)
        if rm and not current["radio"]:
            current["radio"] = rm.group(1).strip()
    if current and current.get("ssid") is not None:
        nets.append(current)
    # de-dupe by ssid keeping strongest signal number if present
    by: dict[str, dict[str, str]] = {}
    for n in nets:
        ssid = n.get("ssid") or ""
        if not ssid:
            continue
        prev = by.get(ssid)
        if not prev:
            by[ssid] = n
            continue
        def pct(s: str) -> int:
            m = re.search(r"(\d+)", s or "")
            return int(m.group(1)) if m else -1
        if pct(n.get("signal", "")) >= pct(prev.get("signal", "")):
            by[ssid] = n
    return list(by.values())


def _win_profile_xml(ssid: str, password: str) -> str:
    esc = xml.sax.saxutils.escape
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
	<name>{esc(ssid)}</name>
	<SSIDConfig>
		<SSID>
			<name>{esc(ssid)}</name>
		</SSID>
	</SSIDConfig>
	<connectionType>ESS</connectionType>
	<connectionMode>manual</connectionMode>
	<MSM>
		<security>
			<authEncryption>
				<authentication>WPA2PSK</authentication>
				<encryption>AES</encryption>
				<useOneX>false</useOneX>
			</authEncryption>
			<sharedKey>
				<keyType>passPhrase</keyType>
				<protected>false</protected>
				<keyMaterial>{esc(password)}</keyMaterial>
			</sharedKey>
		</security>
	</MSM>
</WLANProfile>
"""


def _win_connect(ssid: str, password: str, interface: str | None) -> dict[str, Any]:
    xml = _win_profile_xml(ssid, password)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
        f.write(xml)
        path = f.name
    try:
        add_args = ["netsh", "wlan", "add", "profile", f"filename={path}"]
        if interface:
            add_args.append(f"interface={interface}")
        add = _run(add_args)
        conn_args = ["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"]
        if interface:
            conn_args.append(f"interface={interface}")
        conn = _run(conn_args)
        # wait briefly for DHCP
        time.sleep(4)
        ifaces = _win_interfaces()
        active = next((i for i in ifaces if i.name == interface), None) if interface else None
        if active is None:
            active = next((i for i in ifaces if i.ssid == ssid), None)
        route = _win_ensure_route("192.168.1.254", interface or (active.name if active else None))
        ok = conn.returncode == 0
        return {
            "ok": ok,
            "add_profile": (add.stdout or add.stderr or "").strip(),
            "connect": (conn.stdout or conn.stderr or "").strip(),
            "interface": active.to_dict() if active else None,
            "route": route,
        }
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


def _win_disconnect(interface: str | None) -> dict[str, Any]:
    args = ["netsh", "wlan", "disconnect"]
    if interface:
        args.append(f"interface={interface}")
    p = _run(args)
    return {
        "ok": p.returncode == 0,
        "output": (p.stdout or p.stderr or "").strip(),
    }


def _win_ensure_route(scope_ip: str, interface: str | None) -> dict[str, Any]:
    if not interface:
        # pick interface currently on Andonstar, else Wi-Fi 2-like
        ifaces = _win_interfaces()
        andon = next((i for i in ifaces if i.ssid.lower().startswith("andonstar")), None)
        interface = andon.name if andon else None
        if not interface:
            return {"ok": False, "error": "no interface specified / no Andonstar link"}
    # Resolve ifIndex
    ps_idx = (
        f"(Get-NetAdapter -Name '{interface.replace(chr(39), '')}' "
        f"-ErrorAction SilentlyContinue).ifIndex"
    )
    p = _run(["powershell", "-NoProfile", "-Command", ps_idx])
    idx = (p.stdout or "").strip()
    if not idx.isdigit():
        return {"ok": False, "error": f"could not resolve ifIndex for {interface}", "raw": p.stdout}
    script = (
        f"Remove-NetRoute -DestinationPrefix '{scope_ip}/32' -Confirm:$false "
        f"-ErrorAction SilentlyContinue; "
        f"New-NetRoute -DestinationPrefix '{scope_ip}/32' -InterfaceIndex {idx} "
        f"-NextHop '0.0.0.0' -RouteMetric 1 -ErrorAction Stop | Out-Null; "
        f"'ok'"
    )
    r = _run(["powershell", "-NoProfile", "-Command", script])
    out = (r.stdout or r.stderr or "").strip()
    return {
        "ok": r.returncode == 0 and "ok" in out.lower(),
        "interface": interface,
        "ifIndex": idx,
        "output": out,
        "hint": "If access denied, run the UI elevated once or add the host route manually.",
    }


# ---------- macOS / Linux (best-effort) ----------

def _mac_interfaces() -> list[WifiInterface]:
    # airport / networksetup
    p = _run(["networksetup", "-listallhardwareports"])
    out: list[WifiInterface] = []
    name = dev = None
    for line in (p.stdout or "").splitlines():
        if line.startswith("Hardware Port:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Device:"):
            dev = line.split(":", 1)[1].strip()
            if name and ("Wi-Fi" in name or "AirPort" in name):
                ssid_p = _run(["networksetup", "-getairportnetwork", dev])
                ssid = ""
                m = re.search(r"Current Wi-Fi Network:\s*(.+)", ssid_p.stdout or "")
                if m:
                    ssid = m.group(1).strip()
                out.append(WifiInterface(name=dev, description=name, ssid=ssid, state="unknown"))
            name = None
    return out


def _mac_scan() -> list[dict[str, str]]:
    airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
    p = _run([airport, "-s"], timeout=25)
    nets = []
    for line in (p.stdout or "").splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        # SSID can have spaces; airport output is awkward — best effort
        nets.append({"ssid": parts[0], "signal": parts[1] if len(parts) > 1 else "", "auth": "", "radio": ""})
    return nets


def _mac_connect(ssid: str, password: str, interface: str | None) -> dict[str, Any]:
    dev = interface or "en0"
    p = _run(["networksetup", "-setairportnetwork", dev, ssid, password], timeout=40)
    return {"ok": p.returncode == 0, "output": (p.stdout or p.stderr or "").strip()}


def _linux_interfaces() -> list[WifiInterface]:
    p = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"])
    out = []
    for line in (p.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[1] == "wifi":
            out.append(WifiInterface(name=parts[0], state=parts[2], ssid=parts[3] if parts[3] != "--" else ""))
    return out


def _linux_scan(interface: str | None) -> list[dict[str, str]]:
    args = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"]
    if interface:
        args.extend(["ifname", interface])
    p = _run(args, timeout=25)
    nets = []
    for line in (p.stdout or "").splitlines():
        parts = line.split(":")
        if not parts or not parts[0]:
            continue
        nets.append({
            "ssid": parts[0],
            "signal": (parts[1] + "%") if len(parts) > 1 else "",
            "auth": parts[2] if len(parts) > 2 else "",
            "radio": "",
        })
    return nets


def _linux_connect(ssid: str, password: str, interface: str | None) -> dict[str, Any]:
    args = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
    if interface:
        args.extend(["ifname", interface])
    p = _run(args, timeout=40)
    return {"ok": p.returncode == 0, "output": (p.stdout or p.stderr or "").strip()}


def _linux_disconnect(interface: str | None) -> dict[str, Any]:
    if not interface:
        return {"ok": False, "error": "interface required on Linux"}
    p = _run(["nmcli", "dev", "disconnect", interface])
    return {"ok": p.returncode == 0, "output": (p.stdout or p.stderr or "").strip()}
