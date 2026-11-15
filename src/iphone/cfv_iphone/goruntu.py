"""Ham görüntü: hazırlıkta tamamlanan ürünü `.tamam` + tek rename ile raw/iphone'a koyar (PLAN §3, §11).

Kurallar:
- raw yalnız eklenir: hedef varsa rename yapılmaz (renameat2 RENAME_NOREPLACE), hiçbir şey silinmez/üzerine yazılmaz.
- raw görüntüleri yalnız birbirleriyle inode paylaşır; çalışma kopyasıyla ASLA (idevicebackup2 dosyayı yerinde yeniden
  yazabilir). Bu yüzden kopya `rsync --link-dest=<önceki raw görüntü>` ile hazırlığa alınır.
- borg (server-main'in cron'u) çalışırken rename yapılmaz: 5 dk'da bir bakılır, en çok 2 sa. Sarkarsa ürün hazırlıkta
  `.tamam`'sız kalır, sonraki tur `yayinla` ile tamamlar.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from pathlib import Path

from cfv_iphone import ayar

TAMAM = ".tamam"
URUNLER = ("backup-enc", "extract", "photos-db", "photos-meta")
_AD = re.compile(r"^(?P<onek>[a-z-]+?)-(?P<tarih>\d{4}-\d{2}-\d{2})(?:-(?P<n>\d+))?$")


def ad_coz(ad: str) -> tuple[str, str, int] | None:
    m = _AD.match(ad)
    return (m["onek"], m["tarih"], int(m["n"] or 1)) if m else None


def ek_sec(tarih: str, kokler: list[Path], onekler=URUNLER) -> str:
    """Aynı gün tekrarında tüm ürünler için ortak son ek: '' , '-2', '-3' … (raw ve hazırlıkta boş olan ilk)."""
    n = 1
    while True:
        ek = "" if n == 1 else f"-{n}"
        if not any((k / f"{o}-{tarih}{ek}").exists() for k in kokler for o in onekler):
            return ek
        n += 1


def _sira(d: Path):
    a = ad_coz(d.name)
    return (a[1], a[2]) if a else ("", 0)


def onceki(onek: str, kok: Path | None = None) -> Path | None:
    """raw'daki `.tamam`'lı en yeni <onek>-<tarih>[-n] (sayısal sırayla; '-10' '-2'den sonra)."""
    kok = kok or ayar.raw()
    adaylar = [d for d in kok.glob(f"{onek}-*")
               if d.is_dir() and (d / TAMAM).exists() and (a := ad_coz(d.name)) and a[0] == onek]
    return max(adaylar, key=_sira) if adaylar else None


def kopyala(kaynak: Path, hedef: Path, link_dest: Path | None) -> None:
    """kaynak/ → hedef/ (hedef yok olmalı). Değişmeyen dosyalar önceki raw görüntüsüne hardlink."""
    if hedef.exists():
        raise FileExistsError(hedef)
    hedef.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--no-inc-recursive"]
    if link_dest:
        cmd.append(f"--link-dest={link_dest}")
    cmd += [f"{kaynak}/", f"{hedef}/"]
    subprocess.run(cmd, check=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})


def borg_calisiyor() -> bool:
    return subprocess.run(["pgrep", "-x", "borg"], capture_output=True).returncode == 0


def borg_bekle(azami_sn: int = 2 * 3600, aralik_sn: int = 300, *, kontrol=borg_calisiyor, uyu=time.sleep) -> bool:
    beklenen = 0
    while kontrol():
        if beklenen >= azami_sn:
            return False
        uyu(aralik_sn)
        beklenen += aralik_sn
    return True


_libc = ctypes.CDLL(None, use_errno=True)
_AT_FDCWD, _RENAME_NOREPLACE = -100, 1


def tasi_yerine_yazmadan(kaynak: Path, hedef: Path) -> None:
    """Atomik rename; hedef varsa FileExistsError (hiçbir şeyin üzerine yazılmaz)."""
    r = _libc.renameat2(_AT_FDCWD, os.fsencode(kaynak), _AT_FDCWD, os.fsencode(hedef), _RENAME_NOREPLACE)
    if r != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), str(hedef))


def _syncfs(yol: Path) -> None:
    """Yalnız o dosya sistemini (depo1) diske indir; os.sync() bütün makineyi bekletirdi."""
    fd = os.open(yol, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _libc.syncfs(fd)
    finally:
        os.close(fd)


def bekleyenler(hazirlik: Path | None = None) -> list[Path]:
    """Hazırlıkta yayın bekleyen ürünler (ürün adı kalıbında ve boş olmayan). Sıra: backup-enc önce."""
    hazirlik = hazirlik or ayar.hazirlik()
    if not hazirlik.is_dir():
        return []
    urun = [d for d in hazirlik.iterdir() if d.is_dir() and (a := ad_coz(d.name)) and a[0] in URUNLER
            and any(d.iterdir())]
    return sorted(urun, key=lambda d: (_sira(d), URUNLER.index(ad_coz(d.name)[0])))


def yayinla(urunler: list[Path], raw: Path | None = None, *, bekle=borg_bekle) -> list[Path]:
    """Her ürüne boş `.tamam` yaz ve raw'a taşı. borg bitmezse hiçbirine dokunmaz ([] döner)."""
    raw = raw or ayar.raw()
    if not urunler:
        return []
    if not bekle():
        return []
    yayinlanan = []
    for d in urunler:
        hedef = raw / d.name
        if hedef.exists():
            raise FileExistsError(hedef)
        (d / TAMAM).touch(exist_ok=True)
        _syncfs(d)
        tasi_yerine_yazmadan(d, hedef)
        yayinlanan.append(hedef)
    return yayinlanan
