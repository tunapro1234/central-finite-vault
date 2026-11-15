"""usbmuxd protokolü (netmuxd 127.0.0.1:27015) ve avahi ile telefon bulma.

netmuxd kendi mDNS'iyle değil (--disable-mdns, server-main şartı) çalıştığı için telefonu avahi bulur:
`avahi-browse` (D-Bus → çalışan avahi-daemon) `_apple-mobdev2._tcp` hizmetini çözer, örnek adı `<WiFi MAC>@<id>`.
MAC eşleştirme kaydındaki `WiFiMACAddress` ile UDID'ye bağlanır ve netmuxd'ye `AddDevice` ile bildirilir.
"""

from __future__ import annotations

import plistlib
import socket
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cfv_iphone import ayar

SERVICE = "_apple-mobdev2._tcp"


def _addr(addr: str) -> tuple[str, int]:
    host, port = addr.rsplit(":", 1)
    return host, int(port)


def istek(msg: dict, addr: str = ayar.NETMUXD, timeout: float = 10) -> dict:
    """Tek usbmuxd plist isteği: 16 bayt başlık (uzunluk, sürüm=1, tür=8 plist, etiket) + XML plist."""
    body = plistlib.dumps(msg)
    with socket.create_connection(_addr(addr), timeout=timeout) as s:
        s.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)
        head = _oku(s, 16)
        size = struct.unpack("<I", head[:4])[0]
        return plistlib.loads(_oku(s, size - 16))


def _oku(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("netmuxd bağlantıyı kapattı")
        buf += chunk
    return buf


def cihazlar(addr: str = ayar.NETMUXD) -> list[dict]:
    res = istek({"MessageType": "ListDevices", "ClientVersionString": "cfv-iphone", "ProgName": "cfv-iphone"},
                addr)
    return [d.get("Properties", {}) for d in res.get("DeviceList", [])]


def ag_cihazi_ekle(udid: str, ip: str, addr: str = ayar.NETMUXD) -> bool:
    try:  # eşleştirme kaydı yoksa netmuxd yanıtsız kapatır
        res = istek({"MessageType": "AddDevice", "ConnectionType": "Network", "ServiceName": f"{SERVICE}.local",
                     "IPAddress": ip, "DeviceID": udid}, addr)
    except (ConnectionError, OSError):
        return False
    return res.get("Result") == 1


@dataclass
class Bulunan:
    mac: str
    ip: str


def avahi_ara(timeout: int = 20) -> list[Bulunan]:
    """`avahi-browse -rpt` çıktısı: '=;arayüz;proto;ad;tür;alan;host;adres;port;txt'. IPv4 tercih edilir."""
    try:
        out = subprocess.run(["avahi-browse", "-rpt", "-k", SERVICE], capture_output=True, text=True,
                             timeout=timeout).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    bulunan: dict[str, Bulunan] = {}
    for line in out.splitlines():
        f = line.split(";")
        if len(f) < 9 or f[0] != "=" or "@" not in f[3]:
            continue
        mac, ip = f[3].split("@", 1)[0].lower(), f[7]
        if mac not in bulunan or (":" in bulunan[mac].ip and ":" not in ip):
            bulunan[mac] = Bulunan(mac, ip)
    return list(bulunan.values())


def eslesmis(lockdown: Path = ayar.LOCKDOWN) -> dict[str, str]:
    """Eşleştirme kayıtlarından {wifi_mac: udid}. Kayıt içeriği (anahtarlar) okunur ama hiçbir yere yazılmaz."""
    sonuc = {}
    for p in sorted(lockdown.glob("*.plist")):
        if p.name == "SystemConfiguration.plist":
            continue
        try:
            d = plistlib.loads(p.read_bytes())
        except Exception:
            continue
        if isinstance(d.get("WiFiMACAddress"), str):
            sonuc[d["WiFiMACAddress"].lower()] = d.get("UDID") or p.stem
    return sonuc


def telefonu_bul(udid: str | None = None) -> str | None:
    """Telefon netmuxd'de görünüyorsa UDID; değilse avahi'yle bulup ekler. Yoksa None."""
    gorunen = {d.get("SerialNumber") for d in cihazlar()}
    if udid and udid in gorunen:
        return udid
    macs = eslesmis()
    for b in avahi_ara():
        u = macs.get(b.mac)
        if u and (udid is None or u == udid) and ag_cihazi_ekle(u, b.ip):
            return u
    return None
