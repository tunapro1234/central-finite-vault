"""idevicebackup2 sürücüsü: Wi-Fi (netmuxd) ya da USB (sistem usbmuxd), şifre bekleme + başarı ölçütü (PLAN §3).

iOS 16.1+ her yedekte cihaz şifresi ister. "Requesting backup from device..." satırından sonra çıktı durur; Tuna'ya
laptop-notify (critical) gider. SIFRE_BEKLEME_SN içinde yeni çıktı gelmezse süreç kapatılır → "şifre girilmedi".
"""

from __future__ import annotations

import os
import plistlib
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cfv_iphone import ayar

ISTEK = "Requesting backup from device"
BASARI = "Backup Successful."
SESSIZLIK_SN = 30 * 60      # şifreden sonra bu kadar çıktı yoksa takıldı say


@dataclass
class Sonuc:
    ok: bool
    neden: str                      # "tamam" | "şifre girilmedi" | "takıldı" | "hata: ..." | ölçüt eksikleri
    rc: int | None = None
    sure_sn: int = 0
    tam_yedek: bool | None = None   # Full/Incremental backup mode satırından


def bildir(baslik: str, mesaj: str, seviye: str = "critical") -> None:
    try:
        subprocess.run(["laptop-notify", baslik, mesaj, seviye], timeout=30, capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def dogrula(yer: Path) -> list[str]:
    """Başarı ölçütünün dosya tarafı: Status.plist SnapshotState=finished, Manifest.plist IsEncrypted=True."""
    eksik = []
    try:
        st = plistlib.loads((yer / "Status.plist").read_bytes())
        if st.get("SnapshotState") != "finished":
            eksik.append(f"SnapshotState={st.get('SnapshotState')}")
    except (FileNotFoundError, plistlib.InvalidFileException):
        eksik.append("Status.plist okunamadı")
    try:
        mf = plistlib.loads((yer / "Manifest.plist").read_bytes())
        if mf.get("IsEncrypted") is not True:
            eksik.append("yedek şifreli değil")
    except (FileNotFoundError, plistlib.InvalidFileException):
        eksik.append("Manifest.plist okunamadı")
    return eksik


def _okuyucu(akis, q: queue.Queue) -> None:
    """İlerleme çubuğu \\r ile yazıldığı için hem \\r hem \\n satır sonu sayılır."""
    tampon = b""
    while chunk := akis.read1(4096):
        tampon += chunk
        *satirlar, tampon = re.split(rb"[\r\n]", tampon)
        for s in satirlar:
            if s.strip():
                q.put(s.decode("utf-8", "replace"))
    if tampon.strip():
        q.put(tampon.decode("utf-8", "replace"))
    q.put(None)


def calistir(udid: str, *, usb: bool = False, program: str = "idevicebackup2", sifre_bekleme: int | None = None,
             sessizlik: int = SESSIZLIK_SN, bildirim=bildir, logla=print) -> Sonuc:
    hedef = ayar.calisma()
    hedef.mkdir(parents=True, exist_ok=True)
    sifre_bekleme = sifre_bekleme or ayar.SIFRE_BEKLEME_SN
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C", "HOME": os.environ.get("HOME", "/root")}
    cmd = [program, "-u", udid]
    if not usb:
        cmd.append("-n")
        env["USBMUXD_SOCKET_ADDRESS"] = ayar.NETMUXD
    cmd += ["backup", str(hedef)]
    bas = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_okuyucu, args=(p.stdout, q), daemon=True).start()
    sifre_bekliyor = False
    basari = False
    tam = None
    neden = None
    son_yuzde = -1
    while True:
        try:
            satir = q.get(timeout=sifre_bekleme if sifre_bekliyor else sessizlik)
        except queue.Empty:
            neden = "şifre girilmedi" if sifre_bekliyor else "takıldı"
            os.killpg(p.pid, signal.SIGTERM)
            break
        if satir is None:
            break
        if sifre_bekliyor:
            sifre_bekliyor = False
            logla("şifre girildi, aktarım başladı")
        if ISTEK in satir:
            sifre_bekliyor = True
            logla("yedek istendi, telefonda şifre bekleniyor")
            bildirim("iPhone yedeği", f"Telefonda cihaz şifresini gir ({sifre_bekleme // 60} dk içinde)", "critical")
        elif BASARI in satir:
            basari = True
        elif "Full backup mode" in satir:
            tam = True
        elif "Incremental backup mode" in satir:
            tam = False
        elif (m := re.search(r"\]\s+(\d+)% Finished", satir)) and int(m[1]) // 25 != son_yuzde // 25:
            son_yuzde = int(m[1])
            logla(f"ilerleme %{son_yuzde}")
        elif "rror" in satir or "Failed" in satir:
            logla(f"idevicebackup2: {satir[:200]}")
    try:
        rc = p.wait(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        rc = p.wait()
    sure = int(time.monotonic() - bas)
    if neden:
        return Sonuc(False, neden, rc, sure, tam)
    eksik = [] if rc == 0 else [f"rc={rc}"]
    if not basari:
        eksik.append("'Backup Successful' yok")
    eksik += dogrula(hedef / udid)
    return Sonuc(not eksik, "tamam" if not eksik else "; ".join(eksik), rc, sure, tam)
