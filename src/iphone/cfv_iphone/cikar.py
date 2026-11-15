"""Şifreli yedekten artımlı çözülmüş çıkarım + photos-db + photos-meta (PLAN §3).

Çıktı düzeni eskisiyle aynı: `extract-<tarih>/<Domain>/<relativePath>`; çözülmüş Manifest `extract-<tarih>/_cfv/Manifest.db`.
Artımlı: şifreli blob önceki raw görüntüdekiyle aynı inode'daysa (rsync --link-dest değişmedi dedi) VE Manifest'teki
`file` kaydı birebir aynıysa, önceki çıkarımdaki çözülmüş dosya hardlink'lenir; değilse yeniden çözülür.
Yedek şifresi yalnız bellekte; hiçbir yere yazılmaz, loglanmaz.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from iphone_backup_decrypt import EncryptedBackup, utils

from cfv_iphone import ayar

MANIFEST = Path("_cfv") / "Manifest.db"
YARIM = ".cfv-yarim"
PHOTOS = ("CameraRollDomain", "Media/PhotoData/Photos.sqlite")
WHATSAPP = ayar.GEREKLI["WhatsApp"]


@dataclass
class Sayac:
    dosya: int = 0      # Manifest'te flags=1 (normal dosya)
    haric: int = 0      # HARIC domain'leri
    baglanan: int = 0   # önceki çıkarımdan hardlink
    cozulen: int = 0
    eksik: int = 0      # Manifest'te var, yedekte blob yok
    hata: int = 0


def haric(domain: str) -> bool:
    return any(fnmatch.fnmatchcase(domain, p) for p in ayar.HARIC)


def cikti_yolu(kok: Path, domain: str, rel: str) -> str:
    """Kütüphanenin dizin-aşımı korumalı birleştirmesi (relativePath güvenilmez girdi)."""
    return utils.safe_output_path(str(kok), domain, os.path.dirname(rel), os.path.basename(rel))


def ayni_blob(bk: Path, onceki_bk: Path | None, file_id: str) -> bool:
    if onceki_bk is None:
        return False
    try:
        a = os.stat(utils.backup_file_path(str(bk), file_id))
        b = os.stat(utils.backup_file_path(str(onceki_bk), file_id))
    except FileNotFoundError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _satirlar(manifest: Path, onceki_manifest: Path | None):
    """(fileID, domain, relativePath, file, onceki_file|None) — önceki Manifest ATTACH ile, belleğe yığmadan."""
    conn = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
    if onceki_manifest and onceki_manifest.exists():
        conn.execute("ATTACH DATABASE ? AS eski", (f"file:{onceki_manifest}?mode=ro",))
        q = ("SELECT y.fileID, y.domain, y.relativePath, y.file, e.file FROM Files y "
             "LEFT JOIN eski.Files e ON e.fileID = y.fileID AND e.flags = 1 WHERE y.flags = 1 ORDER BY y.fileID")
    else:
        q = "SELECT fileID, domain, relativePath, file, NULL FROM Files WHERE flags = 1 ORDER BY fileID"
    try:
        yield from conn.execute(q)
    finally:
        conn.close()


def cikar(bk: Path, hedef: Path, sifre: bytes, onceki_bk: Path | None = None,
          onceki_ext: Path | None = None) -> Sayac:
    """bk: hazırlıktaki backup-enc görüntüsü; hedef: hazırlıktaki extract-<tarih> (henüz yok)."""
    if hedef.exists():
        raise FileExistsError(hedef)
    ayar.gecici().mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(ayar.gecici())   # kütüphanenin geçici Manifest'i depo1'de kalsın (kök SSD değil)
    eb = EncryptedBackup(backup_directory=str(bk), passphrase=sifre)
    manifest = hedef / MANIFEST
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        eb.save_manifest_file(str(manifest))      # anahtar kesesini de açar (yanlış şifre → burada hata)
    onceki_manifest = onceki_ext / MANIFEST if onceki_ext else None
    s = Sayac()
    for fid, domain, rel, blob, eski_blob in _satirlar(manifest, onceki_manifest):
        s.dosya += 1
        if haric(domain):
            s.haric += 1
            continue
        out = ""
        try:
            out = cikti_yolu(hedef, domain, rel)
            if os.path.lexists(out):
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if eski_blob is not None and eski_blob == blob and ayni_blob(bk, onceki_bk, fid):
                eski = cikti_yolu(onceki_ext, domain, rel)
                if os.path.isfile(eski):
                    os.link(eski, out)
                    s.baglanan += 1
                    continue
            fp = utils.FilePlist(blob)
            src = utils.backup_file_path(str(bk), fid)
            if not os.path.exists(src):
                s.eksik += 1
                continue
            if fp.encryption_key is None:
                if fp.filesize == 0:
                    Path(out).touch()
                    s.cozulen += 1
                else:
                    s.hata += 1
                continue
            key = eb.keybag.unwrap_key_for_class(fp.protection_class, fp.encryption_key)
            tmp = out + YARIM
            utils.aes_decrypt_chunked(in_filename=src, out_filepath=tmp, key=key)
            if fp.mtime:
                os.utime(tmp, times=(fp.mtime, fp.mtime))
            os.replace(tmp, out)
            s.cozulen += 1
        except Exception as e:  # tek dosya tüm turu düşürmesin; sayı denetimde görünür
            s.hata += 1
            _log_hata(hedef, domain, rel, e)
            if out:   # kendi yarım geçici dosyamız (hazırlıkta), ürüne girmesin
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(out + YARIM)
    eb._cleanup()
    return s


def _log_hata(hedef: Path, domain: str, rel: str, e: Exception) -> None:
    with open(hedef / "_cfv" / "hatalar.txt", "a") as f:
        f.write(f"{domain}\t{rel}\t{type(e).__name__}: {e}\n")


def _wal_birlestir(kaynak: Path, hedef: Path) -> None:
    """kaynak (+ -wal/-shm) → geçici kopya → tek dosyalık (journal_mode=DELETE) hedef. Kaynağa hiçbir şey yazılmaz."""
    with tempfile.TemporaryDirectory(dir=ayar.gecici()) as td:
        kopya = Path(td) / kaynak.name
        for ek in ("", "-wal", "-shm"):
            if (p := Path(f"{kaynak}{ek}")).exists():
                shutil.copy2(p, f"{kopya}{ek}")
        src = sqlite3.connect(kopya)
        dst = sqlite3.connect(hedef)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()


def photos_db(ext: Path, hedef: Path) -> bool:
    kaynak = ext / PHOTOS[0] / PHOTOS[1]
    if not kaynak.exists():
        return False
    hedef.mkdir(parents=True)
    _wal_birlestir(kaynak, hedef / "Photos.sqlite")
    return True


def son_afc(raw: Path | None = None) -> Path | None:
    raw = raw or ayar.raw()
    adaylar = sorted(d for d in raw.glob("afc-*") if (d / "PhotoData/Caches/search/psi.sqlite").exists())
    return adaylar[-1] if adaylar else None


def photos_meta(photos_sqlite: Path, ext: Path, hedef: Path, raw: Path | None = None) -> str:
    """_tools/photos_meta.py + photos_meta_extra.py. psi.sqlite yedekte yok → en yeni afc-*'den (not döner)."""
    raw = raw or ayar.raw()
    afc = son_afc(raw)
    if afc is None:
        return "afc yok, photos-meta üretilmedi"
    hedef.mkdir(parents=True)
    out = hedef / "photos-meta.sqlite"
    with tempfile.TemporaryDirectory(dir=ayar.gecici()) as td:
        psi = Path(td) / "psi.sqlite"
        _wal_birlestir(afc / "PhotoData/Caches/search/psi.sqlite", psi)
        araclar = raw / "_tools"
        subprocess.run([sys.executable, "-I", str(araclar / "photos_meta.py"), str(photos_sqlite), str(psi), str(out)],
                       check=True, capture_output=True, cwd=td)
        wa = ext / WHATSAPP[0] / WHATSAPP[1]
        if wa.exists():
            chat = Path(td) / "ChatStorage.sqlite"
            _wal_birlestir(wa, chat)
            subprocess.run([sys.executable, "-I", str(araclar / "photos_meta_extra.py"), str(photos_sqlite), str(chat),
                            str(out)], check=True, capture_output=True, cwd=td)
    return f"psi kaynağı {afc.name}"


def sayac_dict(s: Sayac) -> dict:
    return asdict(s)
