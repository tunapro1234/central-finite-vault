"""Denetim (PLAN §5) + durum/log yazımı. Çıktıda kişisel içerik yok: yalnız sayılar, tarihler, dosya adları."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from cfv_iphone import ayar
from cfv_iphone.cikar import MANIFEST

APPLE_EPOCH = 978307200
ZEPP = ("AppDomain-com.huami.watch", "Documents/*/HMCorePersistanceDatabaseV1.sqlite")
TAZELIK_GUN = 7
HATA_ESIGI = 0.05

# (ad, sorgu, birim): birim 's' = 2001'den saniye, 'ns' = 2001'den nanosaniye (sms.db iOS 11+)
SON_KAYIT = {
    "CallHistory": ("SELECT max(ZDATE) FROM ZCALLRECORD", "s"),
    "Health": ("SELECT max(start_date) FROM samples", "s"),
    "WhatsApp": ("SELECT max(ZMESSAGEDATE) FROM ZWAMESSAGE", "s"),
    "SMS": ("SELECT max(date) FROM message", "ns"),
}


# ---------- kayıt ----------

def log(satir: str) -> None:
    ayar.logs().mkdir(parents=True, exist_ok=True)
    zaman = dt.datetime.now(ayar.TZ).isoformat(timespec="seconds")
    with open(ayar.logs() / ayar.LOG, "a") as f:
        f.write(f"{zaman} {satir}\n")


def durum_oku() -> dict:
    try:
        return json.loads((ayar.logs() / ayar.DURUM).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def durum_yaz(**alanlar) -> dict:
    d = durum_oku() | alanlar
    ayar.logs().mkdir(parents=True, exist_ok=True)
    yol = ayar.logs() / ayar.DURUM
    tmp = yol.with_suffix(".json.yeni")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str) + "\n")
    os.replace(tmp, yol)
    return d


# ---------- denetim ----------

def dosya_sayisi(ext: Path) -> int | None:
    m = ext / MANIFEST
    if not m.exists():
        return None
    with contextlib.closing(sqlite3.connect(f"file:{m}?mode=ro", uri=True)) as c:
        return c.execute("SELECT count(*) FROM Files WHERE flags = 1").fetchone()[0]


def gerekli_yollar(ext: Path) -> dict[str, Path | None]:
    sonuc: dict[str, Path | None] = {}
    for ad, (dom, rel) in ayar.GEREKLI.items():
        p = ext / dom / rel
        sonuc[ad] = p if p.exists() else None
    zepp = sorted((ext / ZEPP[0]).glob(ZEPP[1]))
    sonuc["Zepp"] = zepp[0] if zepp else None
    return sonuc


@contextlib.contextmanager
def ac(db: Path):
    """Kaynağa hiçbir şey yazmadan bağlantı: dolu -wal varsa geçici kopyada (WAL dahil), yoksa immutable."""
    wal = Path(f"{db}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        ayar.gecici().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ayar.gecici()) as td:
            kopya = Path(td) / db.name
            shutil.copy2(db, kopya)
            shutil.copy2(wal, f"{kopya}-wal")
            c = sqlite3.connect(kopya)
            try:
                yield c
            finally:
                c.close()
    else:
        c = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        try:
            yield c
        finally:
            c.close()


def _zaman(deger, birim: str) -> str | None:
    if deger is None:
        return None
    sn = deger / 1e9 if birim == "ns" or deger > 1e12 else deger
    return dt.datetime.fromtimestamp(sn + APPLE_EPOCH, ayar.TZ).isoformat(timespec="minutes")


def denetle(ext: Path, onceki_ext: Path | None, sayac: dict | None = None, tarih: dt.date | None = None) -> dict:
    """{'dur': [...], 'uyari': [...], ...}. 'dur' doluysa ürün yayınlanmaz (PLAN §5)."""
    tarih = tarih or dt.datetime.now(ayar.TZ).date()
    dur: list[str] = []
    uyari: list[str] = []
    n = dosya_sayisi(ext)
    m = dosya_sayisi(onceki_ext) if onceki_ext else None
    if n is None:
        dur.append("çözülmüş Manifest yok")
    elif m and n < m * (1 - ayar.DUSUS_ESIGI):
        dur.append(f"dosya sayısı {m} → {n} (%{100 * (m - n) / m:.1f} düşüş)")
    if sayac and sayac.get("dosya"):
        bozuk = sayac.get("eksik", 0) + sayac.get("hata", 0)
        if bozuk > sayac["dosya"] * HATA_ESIGI:
            dur.append(f"{bozuk} dosya çözülemedi/eksik (> %{HATA_ESIGI * 100:.0f})")
        elif bozuk:
            uyari.append(f"{bozuk} dosya çözülemedi/eksik (ayrıntı _cfv/hatalar.txt)")

    gerekli = gerekli_yollar(ext)
    kontrol: dict[str, str] = {}
    son: dict[str, str | None] = {}
    for ad, yol in gerekli.items():
        if yol is None:
            uyari.append(f"{ad} dosyası yok")
            continue
        try:
            with ac(yol) as c:
                kontrol[ad] = c.execute("PRAGMA quick_check").fetchone()[0]
                if ad in SON_KAYIT:
                    q, birim = SON_KAYIT[ad]
                    son[ad] = _zaman(c.execute(q).fetchone()[0], birim)
        except sqlite3.Error as e:
            # Apple'ın kendi FTS tokenizer'ı (ör. AddressBook ab_cf_tokenizer) burada yok: bozukluk değil, atla
            kontrol[ad] = "atlandı (özel FTS tokenizer)" if "unknown tokenizer" in str(e) else f"hata: {e}"
        if kontrol[ad] not in ("ok", "atlandı (özel FTS tokenizer)"):
            uyari.append(f"{ad} quick_check: {kontrol[ad]}")
    if gerekli.get("Zepp"):
        son["Zepp (dosya)"] = dt.datetime.fromtimestamp(gerekli["Zepp"].stat().st_mtime, ayar.TZ).isoformat(
            timespec="minutes")
    for ad, z in son.items():
        if z and (tarih - dt.date.fromisoformat(z[:10])).days > TAZELIK_GUN:
            uyari.append(f"{ad}: son kayıt {z[:10]} ({(tarih - dt.date.fromisoformat(z[:10])).days} gün)")
    return {"dur": dur, "uyari": uyari, "dosya_sayisi": n, "onceki_dosya_sayisi": m, "quick_check": kontrol,
            "son_kayit": son}
