"""cfv-iphone: haftalık şifreli iPhone yedeği → ham görüntü → çözülmüş çıkarım (PLAN §3, §5, §7).

Tuna'nın bir kerelik komutları (telefon USB'de, sunucu terminalinde root):
  cfv-iphone eslestir      güven + Wi-Fi eşitleme + netmuxd'ye eşleştirme kaydı
  cfv-iphone sifre-kur     yedek şifresini sakla + telefonda yedek şifrelemeyi aç
  cfv-iphone yedek --usb   ilk tam şifreli yedek
Servis (cfv-iphone-yedek.timer, her akşam 20:00):
  cfv-iphone tur           bu haftanın Pazar'ından beri başarılı yedek yoksa 23:30'a kadar dener
Diğer: yedek (Wi-Fi), yayinla (hazırlıkta bekleyenleri raw'a koy), durum.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import getpass
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cfv_iphone import ayar, goruntu, mux
from cfv_iphone.denetim import denetle, durum_oku, durum_yaz, log

PY_ARAC = Path(sys.executable).parent


def _kilit():
    ayar.work().mkdir(parents=True, exist_ok=True)
    f = open(ayar.work() / ".iphone.kilit", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("başka bir cfv-iphone turu çalışıyor", file=sys.stderr)
        sys.exit(75)
    return f


def _bp(mesaj: str) -> None:
    try:
        subprocess.run(["bp", "msg", "server-cfv", f"[cfv-iphone] {mesaj}"], timeout=60, capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _udid() -> str | None:
    u = sorted(set(mux.eslesmis().values()))
    return u[0] if len(u) == 1 else None


def _durdurulan() -> Path:
    return ayar.hazirlik() / "durduruldu"


def _yapim() -> Path:
    """Ürünler burada üretilir; yalnız tamamı bitip denetimden geçince hazirlik/ üstüne alınır.
    Böylece çökmüş bir turun yarım ürünü `yayinla`ya hiç görünmez."""
    return ayar.hazirlik() / "yapim"


def _kenara(dizinler) -> list[str]:
    """Yayınlanmayacak ürünleri silmeden hazirlik/durduruldu/ altına al."""
    _durdurulan().mkdir(parents=True, exist_ok=True)
    adlar = []
    for d in dizinler:
        if d.exists():
            hedef = _durdurulan() / d.name
            if hedef.exists():
                hedef = _durdurulan() / f"{d.name}.{int(time.time())}"
            goruntu.tasi_yerine_yazmadan(d, hedef)
            adlar.append(hedef.name)
    return adlar


# ---------- yedekten sonra: görüntü, çıkarım, denetim, yayın ----------

def isle(udid: str) -> bool:
    raw, haz, yap = ayar.raw(), ayar.hazirlik(), _yapim()
    if yap.exists() and (kalan := list(yap.iterdir())):
        log("çökmüş turdan kalan yarım ürünler kenara: " + ", ".join(_kenara(kalan)))
    yap.mkdir(parents=True, exist_ok=True)
    tarih = ayar.bugun()
    ek = goruntu.ek_sec(tarih, [raw, haz, _durdurulan(), yap])
    ad = lambda onek: yap / f"{onek}-{tarih}{ek}"  # noqa: E731
    onceki_bk = goruntu.onceki("backup-enc")
    onceki_ext = None
    if onceki_bk:
        aday = raw / ("extract-" + onceki_bk.name.removeprefix("backup-enc-"))
        onceki_ext = aday if (aday / goruntu.TAMAM).exists() else None
    bk = ad("backup-enc")
    log(f"görüntü {bk.name} (link-dest {onceki_bk.name if onceki_bk else '-'})")
    goruntu.kopyala(ayar.calisma() / udid, bk, onceki_bk)
    urunler = [bk]

    from cfv_iphone import cikar  # ağır içe aktarım yalnız gerektiğinde
    ext = ad("extract")
    try:
        sifre = ayar.SIFRE.read_bytes().rstrip(b"\r\n")
        sayac = cikar.sayac_dict(cikar.cikar(bk, ext, sifre, onceki_bk, onceki_ext))
        del sifre
    except Exception as e:
        log(f"çıkarım başarısız: {type(e).__name__}: {e}")
        if ext.exists():
            log("yarım çıkarım kenara: " + ", ".join(_kenara([ext])))
        durum_yaz(son_hata=f"çıkarım: {type(e).__name__}")
        _bp(f"çıkarım başarısız ({type(e).__name__}); şifreli görüntü yine de yayınlanıyor, çıkarım yok")
        return _yayinla_kaydet(urunler, None, None)
    log(f"çıkarım {ext.name}: {sayac}")
    den = denetle(ext, onceki_ext, sayac)
    log(f"denetim: dur={den['dur']} uyarı={den['uyari']}")
    if den["dur"]:
        _kenara([bk, ext])
        durum_yaz(son_hata="denetim DUR", denetim=den)
        _bp(f"DENETİM DUR: {'; '.join(den['dur'])}. Ürünler hazirlik/durduruldu/ içinde, raw'a konmadı.")
        return False
    urunler.append(ext)
    pdb = ad("photos-db")
    if cikar.photos_db(ext, pdb):
        urunler.append(pdb)
        pm = ad("photos-meta")
        try:
            log(f"photos-meta: {cikar.photos_meta(pdb / 'Photos.sqlite', ext, pm)}")
            if pm.exists():
                urunler.append(pm)
        except subprocess.CalledProcessError as e:
            log(f"photos-meta başarısız rc={e.returncode}")
            den["uyari"].append("photos-meta üretilemedi")
    if den["uyari"]:
        _bp("yedek tamam, uyarılar: " + "; ".join(den["uyari"])[:900])
    return _yayinla_kaydet(urunler, sayac, den)


def _yayinla_kaydet(urunler: list[Path], sayac: dict | None, den: dict | None) -> bool:
    hazir = []
    for d in urunler:   # yapim/ → hazirlik/ (tamamlandı; borg bitmezse burada bekler)
        if d.parent == _yapim():
            goruntu.tasi_yerine_yazmadan(d, ayar.hazirlik() / d.name)
            d = ayar.hazirlik() / d.name
        hazir.append(d)
    urunler = hazir
    yayin = goruntu.yayinla(urunler)
    if not yayin:
        log("borg 2 sa içinde bitmedi: ürünler hazırlıkta, sonraki tur yayınlar")
        _bp("borg 2 saatte bitmedi; ürünler data/work/hazirlik'te .tamam'sız bekliyor (sonraki tur ya da "
            "`cfv-iphone yayinla`)")
        durum_yaz(son_urunler=[p.name for p in urunler], yayinda=False, sayac=sayac, denetim=den)
    else:
        log("yayınlandı: " + ", ".join(p.name for p in yayin))
        durum_yaz(son_urunler=[p.name for p in yayin], yayinda=True, sayac=sayac, denetim=den,
                  yayin_zamani=dt.datetime.now(ayar.TZ).isoformat(timespec="seconds"))
    if yayin:
        _buyume(yayin)
    return True


def yeni_bayt(kok: Path) -> int:
    """Önceki görüntüyle paylaşılmayan (hardlink sayısı 1) dosyaların toplamı = bu yedeğin diske eklediği."""
    t = 0
    for dp, _, fs in os.walk(kok):
        for f in fs:
            st = os.lstat(os.path.join(dp, f))
            if st.st_nlink == 1:
                t += st.st_size
    return t


def _buyume(yayin: list[Path]) -> None:
    """PLAN §10: 4 haftalık ortalama > 3 GB/hafta ya da depo1 boş < 150 GB → uyarı (server-cfv; server-main'e iletir)."""
    gb = sum(yeni_bayt(p) for p in yayin) / 1e9
    gecmis = (durum_oku().get("buyume_gb") or [])[-3:] + [round(gb, 2)]
    bos = shutil.disk_usage(ayar.data()).free / 1e9
    durum_yaz(buyume_gb=gecmis, depo1_bos_gb=round(bos))
    log(f"büyüme {gb:.2f} GB, son {len(gecmis)} hafta ort. {sum(gecmis) / len(gecmis):.2f} GB, depo1 boş {bos:.0f} GB")
    sorun = []
    if len(gecmis) >= 4 and sum(gecmis) / 4 > ayar.BUYUME_GB:
        sorun.append(f"iPhone ham verisi 4 haftadır ortalama {sum(gecmis) / 4:.1f} GB/hafta büyüyor")
    if bos < ayar.DEPO1_BOS_GB:
        sorun.append(f"depo1 boş yer {bos:.0f} GB")
    if sorun:
        _bp("DİSK: " + "; ".join(sorun) + " (server-main'e de iletilmeli, PLAN §10)")


# ---------- komutlar ----------

def k_yedek(a) -> int:
    kilit = _kilit()  # noqa: F841
    udid = _udid()
    if not udid:
        print("eşleştirme kaydı yok ya da birden fazla: önce `cfv-iphone eslestir`", file=sys.stderr)
        return 2
    if not a.usb and not mux.telefonu_bul(udid):
        print("telefon Wi-Fi'da görünmüyor (aynı ağda ve kilidi açık mı?)", file=sys.stderr)
        return 3
    return 0 if _yedek_ve_isle(udid, usb=a.usb) == "tamam" else 1


def _yedek_ve_isle(udid: str, *, usb: bool) -> str:
    from cfv_iphone import yedek
    log(f"yedek başlıyor ({'USB' if usb else 'Wi-Fi'})")
    durum_yaz(son_deneme=dt.datetime.now(ayar.TZ).isoformat(timespec="seconds"))
    s = yedek.calistir(udid, usb=usb, logla=log)
    log(f"yedek: {s.neden} rc={s.rc} süre={s.sure_sn}s tam={s.tam_yedek}")
    if not s.ok:
        durum_yaz(son_hata=s.neden)
        return s.neden
    durum_yaz(son_basarili=dt.datetime.now(ayar.TZ).isoformat(timespec="seconds"), son_hata=None,
              basarisiz_aksam=0, son_yedek_sn=s.sure_sn)
    isle(udid)
    return "tamam"


def k_tur(a) -> int:
    kilit = _kilit()  # noqa: F841
    if bek := goruntu.bekleyenler():
        log("önceki turdan bekleyenler yayınlanıyor: " + ", ".join(d.name for d in bek))
        _yayinla_kaydet(bek, None, None)
    simdi = dt.datetime.now(ayar.TZ)
    son = durum_oku().get("son_basarili")
    if son and dt.datetime.fromisoformat(son).date() >= ayar.hafta_basi(simdi.date()) and not a.zorla:
        return 0
    udid = _udid()
    if not udid:
        log("tur: eşleştirme kaydı yok")
        durum_yaz(son_hata="eşleştirme yok")
        return 2
    son_an = dt.datetime.combine(simdi.date(), ayar.PENCERE_SONU, ayar.TZ)
    neden, deneme = "telefon görünmedi", 0
    while dt.datetime.now(ayar.TZ) < son_an and deneme < 2:
        if mux.telefonu_bul(udid):
            deneme += 1
            neden = _yedek_ve_isle(udid, usb=False)
            if neden in ("tamam", "şifre girilmedi"):
                break
        time.sleep(ayar.ARAMA_ARALIGI_SN)
    if neden == "tamam":
        return 0
    n = int(durum_oku().get("basarisiz_aksam") or 0) + 1
    durum_yaz(basarisiz_aksam=n, son_hata=neden)
    log(f"akşam başarısız ({n}. kez): {neden}")
    if n % ayar.BASARISIZ_UYARI == 0:
        _bp(f"{n} akşamdır iPhone yedeği alınamadı, son neden: {neden}")
    return 1


def k_yayinla(a) -> int:
    kilit = _kilit()  # noqa: F841
    bek = goruntu.bekleyenler()
    if not bek:
        print("hazırlıkta bekleyen ürün yok")
        return 0
    return 0 if _yayinla_kaydet(bek, None, None) and goruntu.bekleyenler() == [] else 1


def ikinci_kopya(yayin_zamani: str | None, durum_dosyasi: Path = ayar.BORG_DURUM) -> str:
    """PLAN §11 (server-main 2026-11-15: ikinci kopya yoksa yedek 'bitti' sayılmaz): son yayından SONRA biten başarılı
    bir borg data turu varsa ürünler ikinci kopyada. Yalnız server-main'in durum dosyası okunur."""
    if not yayin_zamani:
        return "yayın yok"
    try:
        b = json.loads(durum_dosyasi.read_text())
    except (OSError, json.JSONDecodeError):
        return "borg durumu okunamadı"
    if b.get("exit") == 0 and b.get("ts", 0) > dt.datetime.fromisoformat(yayin_zamani).timestamp():
        return "tamam (borg " + dt.datetime.fromtimestamp(b["ts"], ayar.TZ).isoformat(timespec="minutes") + ")"
    return "bekliyor (sonraki 03:30 borg turu)"


def k_durum(a) -> int:
    d = durum_oku()
    d["hazirlikta"] = [p.name for p in goruntu.bekleyenler()]
    d["durdurulan"] = sorted(p.name for p in _durdurulan().glob("*")) if _durdurulan().exists() else []
    try:
        d["netmuxd_cihaz"] = len(mux.cihazlar())
    except OSError as e:
        d["netmuxd_cihaz"] = f"erişilemedi: {e}"
    d["eslesmis"] = len(mux.eslesmis())
    d["sifre_dosyasi"] = ayar.SIFRE.exists()
    d["ikinci_kopya"] = ikinci_kopya(d.get("yayin_zamani"))
    print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    return 0


def k_eslestir(a) -> int:
    """USB'de: güven → doğrula → Wi-Fi eşitleme → kaydı netmuxd'ye kopyala. Kayıt içeriği ekrana basılmaz."""
    print("iPhone'u USB ile tak ve kilidini aç. Telefonda 'Bu bilgisayara güven' → Güven → cihaz şifresi.")
    for _ in range(40):
        r = subprocess.run(["idevicepair", "pair"], capture_output=True, text=True)
        if "SUCCESS" in r.stdout:
            break
        time.sleep(3)
    else:
        print("eşleştirme olmadı: telefon takılı ve kilidi açık mı, 'Güven' seçildi mi?", file=sys.stderr)
        return 1
    udid = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True).stdout.split()[0]
    if "SUCCESS" not in subprocess.run(["idevicepair", "-u", udid, "validate"], capture_output=True, text=True).stdout:
        print("doğrulama başarısız", file=sys.stderr)
        return 1
    print("eşleşti. Wi-Fi eşitleme açılıyor…")
    r = subprocess.run([str(PY_ARAC / "pymobiledevice3"), "lockdown", "wifi-connections", "on", "--udid", udid],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Wi-Fi eşitleme açılamadı (rc={r.returncode})", file=sys.stderr)
        return 1
    kayit = ayar.LOCKDOWN / f"{udid}.plist"
    if not isinstance(plistlib.loads(kayit.read_bytes()).get("WiFiMACAddress"), str):
        print("uyarı: eşleştirme kaydında Wi-Fi MAC yok; Wi-Fi'da bulma çalışmayabilir", file=sys.stderr)
    hedef = ayar.NETMUXD_STATE
    st = hedef.stat()
    for kaynak in (kayit, ayar.LOCKDOWN / "SystemConfiguration.plist"):
        tmp = hedef / f".{kaynak.name}.yeni"
        shutil.copyfile(kaynak, tmp)
        os.chmod(tmp, 0o600)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, hedef / kaynak.name)
    subprocess.run(["systemctl", "restart", "cfv-netmuxd.service"], check=True)
    print("tamam: eşleştirme kaydı netmuxd'de. Sıradaki: cfv-iphone sifre-kur")
    return 0


def k_sifre_kur(a) -> int:
    """Yedek şifresini sunucuda sakla (root 600), sonra idevicebackup2 ile telefonda şifrelemeyi aç."""
    udid = _udid()
    if not udid:
        print("önce `cfv-iphone eslestir`", file=sys.stderr)
        return 2
    if ayar.SIFRE.exists() and not a.yeniden:
        print("sunucuda kayıtlı yedek şifresi var, o kullanılacak (yarım kalan kurulumu sürdürme). Şimdi", end=" ")
    else:
        s1 = getpass.getpass("Yeni yedek şifresi: ")
        s2 = getpass.getpass("Tekrar: ")
        if s1 != s2 or len(s1) < 8:
            print("eşleşmedi ya da 8 karakterden kısa", file=sys.stderr)
            return 1
        ayar.SIFRE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = ayar.SIFRE.with_suffix(".yeni")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(s1)
        os.replace(tmp, ayar.SIFRE)
        del s1, s2
        print("sunucuda saklandı. Şimdi", end=" ")
    print("idevicebackup2 AYNI şifreyi bir kez daha soracak, "
          "sonra telefonda cihaz şifresi istenecek.")
    r = subprocess.run(["idevicebackup2", "-u", udid, "-i", "encryption", "on"])
    if r.returncode != 0:
        print("telefonda şifreleme açılamadı. Şifreleme daha önce başka bir şifreyle açıldıysa telefonun yedek şifresi "
              "o eski şifredir: bunu cfv-iphone'a (server-cfv) yaz, değiştirmeyi birlikte yapalım.", file=sys.stderr)
        return 1
    print("tamam. Sıradaki: cfv-iphone yedek --usb (ilk tam yedek, ~40 dk, telefon takılı kalsın)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cfv-iphone", description=__doc__.splitlines()[0])
    alt = p.add_subparsers(dest="komut", required=True)
    alt.add_parser("eslestir").set_defaults(f=k_eslestir)
    s = alt.add_parser("sifre-kur")
    s.add_argument("--yeniden", action="store_true", help="sunucudaki kayıtlı şifreyi yenisiyle değiştir")
    s.set_defaults(f=k_sifre_kur)
    s = alt.add_parser("yedek")
    s.add_argument("--usb", action="store_true")
    s.set_defaults(f=k_yedek)
    s = alt.add_parser("tur")
    s.add_argument("--zorla", action="store_true", help="bu hafta başarılı yedek olsa da dene")
    s.set_defaults(f=k_tur)
    alt.add_parser("yayinla").set_defaults(f=k_yayinla)
    alt.add_parser("durum").set_defaults(f=k_durum)
    a = p.parse_args(argv)
    os.umask(0o077)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
