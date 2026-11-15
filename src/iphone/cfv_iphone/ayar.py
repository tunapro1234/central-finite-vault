"""Yollar ve sabitler. Kişisel veri yalnız depo1'de (CFV_DATA); kök SSD'ye yazılmaz."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Istanbul")


def data() -> Path:
    return Path(os.environ.get("CFV_DATA", "/mnt/depo1/cfv/data"))


def raw() -> Path:
    return data() / "raw" / "iphone"


def work() -> Path:
    return data() / "work"


def calisma() -> Path:
    """idevicebackup2'nin artımlı yazdığı çalışma kopyası (ham değil): <bu>/<UDID>/."""
    return work() / "iphone-yedek"


def hazirlik() -> Path:
    """Ürünler burada tamamlanır, sonra tek rename ile raw'a geçer (borg data/work'ü almaz)."""
    return work() / "hazirlik"


def gecici() -> Path:
    """Çözme kütüphanesinin geçici Manifest.db'si (TMPDIR) — /tmp kök SSD olduğu için depo1'de."""
    return work() / "iphone-tmp"


def logs() -> Path:
    return data() / "logs"


DURUM = "iphone-durum.json"
LOG = "iphone-yedek.log"
SIFRE = Path("/root/.config/cfv-iphone/yedek-sifresi")      # root 600; değeri hiçbir yere yazılmaz
LOCKDOWN = Path("/var/lib/lockdown")                         # sistem usbmuxd eşleştirme kayıtları
NETMUXD_STATE = Path("/var/lib/private/cfv-netmuxd")          # cfv-netmuxd.service StateDirectory (DynamicUser)
NETMUXD = "127.0.0.1:27015"
BORG_DURUM = Path("/var/lib/borg-status/data.json")         # server-main'in; yalnız okunur

# Pencere: timer her akşam 20:00; bu haftanın Pazar'ından beri başarılı yedek yoksa dener (Pazar + telafi akşamları).
# Sabit gün çapası: geç biten bir yedek takvimi kaydırmaz.
YEDEK_GUNU = 6                 # date.weekday(): 6 = Pazar
PENCERE_SONU = dt.time(23, 30)
ARAMA_ARALIGI_SN = 15 * 60
SIFRE_BEKLEME_SN = 30 * 60
BASARISIZ_UYARI = 3            # art arda başarısız akşam → bp msg

# Çıkarıma girmeyen domain'ler: anahtar zinciri ve parola/2FA uygulamaları (PLAN §2)
HARIC = ("KeychainDomain", "*me.proton.pass*", "*com.google.Authenticator*", "*com.apple.Passwords*")

# Büyüme/boş yer uyarısı (PLAN §10)
BUYUME_GB = 3.0
DEPO1_BOS_GB = 150

# Denetim (PLAN §5)
DUSUS_ESIGI = 0.05
GEREKLI = {
    "CallHistory": ("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata"),
    "Health": ("HealthDomain", "Health/healthdb_secure.sqlite"),
    "SMS": ("HomeDomain", "Library/SMS/sms.db"),
    "WhatsApp": ("AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared", "ChatStorage.sqlite"),
    "Notlar": ("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite"),
    "Rehber": ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb"),
    "Fotoğraflar": ("CameraRollDomain", "Media/PhotoData/Photos.sqlite"),
}


def bugun() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def hafta_basi(gun: dt.date) -> dt.date:
    """gun'e eşit ya da ondan önceki en yakın yedek günü (Pazar)."""
    return gun - dt.timedelta(days=(gun.weekday() - YEDEK_GUNU) % 7)
