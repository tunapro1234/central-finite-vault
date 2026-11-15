"""Sentetik şifreli yedek: artımlı çıkarımın bağla/çöz kararı, hariç domain'ler, denetim düşüş eşiği.

Gerçek anahtar kesesi yerine sahte EncryptedBackup kullanılır; dosya şifrelemesi gerçek (AES-CBC, sıfır IV, PKCS7),
çözme kütüphanenin kendi aes_decrypt_chunked'ı ile yapılır.
"""

import os
import plistlib
import shutil
import sqlite3
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from cfv_iphone import cikar, denetim

ANAHTAR = bytes(range(32))


def _sifrele(veri: bytes) -> bytes:
    pad = 16 - len(veri) % 16
    return AES.new(ANAHTAR, AES.MODE_CBC, iv=b"\0" * 16).encrypt(veri + bytes([pad]) * pad)


def _file_blob(boyut: int, mtime: int, anahtar_no: int = 1) -> bytes:
    """Manifest.db Files.file: NSKeyedArchiver bplist (MBFile)."""
    return plistlib.dumps({
        "$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null",
                     {"LastModified": mtime, "Size": boyut, "ProtectionClass": 3, "Mode": 0o100644,
                      "EncryptionKey": plistlib.UID(2)},
                     {"NS.data": b"\x03\0\0\0" + bytes([anahtar_no]) * 40}]}, fmt=plistlib.FMT_BINARY)


def _fid(kisa: str) -> str:
    """Kütüphane fileID'nin 40 haneli hex (SHA-1) olmasını şart koşar."""
    return kisa.ljust(40, "0")


def _yedek(kok: Path, dosyalar: dict, link_from: Path | None = None) -> Path:
    """dosyalar: {fileID: (domain, rel, icerik, mtime)}. link_from verilirse aynı blob'lar ondan hardlink."""
    kok.mkdir(parents=True)
    db = sqlite3.connect(kok / "Manifest.plain.db")
    db.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
    for kisa, (dom, rel, icerik, mtime) in dosyalar.items():
        fid = _fid(kisa)
        db.execute("INSERT INTO Files VALUES (?,?,?,1,?)", (fid, dom, rel, _file_blob(len(icerik), mtime)))
        blob = kok / fid[:2] / fid
        blob.parent.mkdir(exist_ok=True)
        eski = link_from / fid[:2] / fid if link_from else None
        if eski and eski.exists() and eski.read_bytes() == _sifrele(icerik):
            os.link(eski, blob)
        else:
            blob.write_bytes(_sifrele(icerik))
    db.commit()
    db.close()
    return kok


class _Keybag:
    def unwrap_key_for_class(self, cls, key):
        return ANAHTAR


class SahteYedek:
    def __init__(self, *, backup_directory, passphrase):
        assert passphrase == b"dogru-sifre"
        self.d = Path(backup_directory)
        self.keybag = _Keybag()

    def save_manifest_file(self, out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy(self.d / "Manifest.plain.db", out)

    def _cleanup(self):
        pass


@pytest.fixture(autouse=True)
def _sahte(monkeypatch, tmp_path):
    monkeypatch.setattr(cikar, "EncryptedBackup", SahteYedek)
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))


def _ilk():
    return {"aa01": ("HomeDomain", "Library/SMS/sms.db", b"sms v1", 100),
            "bb02": ("KeychainDomain", "keychain-backup.plist", b"gizli", 100),
            "cc03": ("AppDomain-com.example.app", "Documents/a.txt", b"ayni kalir", 100),
            "dd04": ("AppDomain-me.proton.pass", "x.db", b"parola", 100)}


def test_ilk_cikarim_hepsini_cozer_hariclari_atlar(tmp_path):
    bk = _yedek(tmp_path / "bk1", _ilk())
    s = cikar.cikar(bk, tmp_path / "ext1", b"dogru-sifre")
    assert (s.dosya, s.haric, s.cozulen, s.baglanan, s.hata) == (4, 2, 2, 0, 0)
    assert (tmp_path / "ext1/HomeDomain/Library/SMS/sms.db").read_bytes() == b"sms v1"
    assert not (tmp_path / "ext1/KeychainDomain").exists()
    assert not (tmp_path / "ext1/AppDomain-me.proton.pass").exists()
    assert (tmp_path / "ext1" / cikar.MANIFEST).exists()
    assert not list((tmp_path / "ext1").rglob("*" + cikar.YARIM))


def test_artimli_degismeyeni_baglar_degiseni_cozer(tmp_path):
    bk1 = _yedek(tmp_path / "bk1", _ilk())
    cikar.cikar(bk1, tmp_path / "ext1", b"dogru-sifre")
    yeni = _ilk() | {"aa01": ("HomeDomain", "Library/SMS/sms.db", b"sms v2 daha uzun", 200)}
    bk2 = _yedek(tmp_path / "bk2", yeni, link_from=bk1)      # rsync --link-dest gibi
    s = cikar.cikar(bk2, tmp_path / "ext2", b"dogru-sifre", bk1, tmp_path / "ext1")
    assert (s.cozulen, s.baglanan) == (1, 1)
    a, b = tmp_path / "ext1/AppDomain-com.example.app/Documents/a.txt", \
        tmp_path / "ext2/AppDomain-com.example.app/Documents/a.txt"
    assert a.stat().st_ino == b.stat().st_ino
    assert (tmp_path / "ext2/HomeDomain/Library/SMS/sms.db").read_bytes() == b"sms v2 daha uzun"
    assert (tmp_path / "ext1/HomeDomain/Library/SMS/sms.db").read_bytes() == b"sms v1"   # eski el değmedi


def test_inode_farkliysa_manifest_ayni_olsa_da_cozer(tmp_path):
    bk1 = _yedek(tmp_path / "bk1", _ilk())
    cikar.cikar(bk1, tmp_path / "ext1", b"dogru-sifre")
    bk2 = _yedek(tmp_path / "bk2", _ilk())                   # aynı içerik ama hardlink değil
    s = cikar.cikar(bk2, tmp_path / "ext2", b"dogru-sifre", bk1, tmp_path / "ext1")
    assert (s.cozulen, s.baglanan) == (2, 0)


def test_eksik_blob_sayilir(tmp_path):
    bk = _yedek(tmp_path / "bk1", _ilk())
    (bk / "aa" / _fid("aa01")).unlink()
    s = cikar.cikar(bk, tmp_path / "ext1", b"dogru-sifre")
    assert s.eksik == 1


def test_hedef_varsa_reddeder(tmp_path):
    bk = _yedek(tmp_path / "bk1", _ilk())
    (tmp_path / "ext1").mkdir()
    with pytest.raises(FileExistsError):
        cikar.cikar(bk, tmp_path / "ext1", b"dogru-sifre")


def test_denetim_dusus_durdurur(tmp_path):
    bk1 = _yedek(tmp_path / "bk1", {f"{i:04x}": ("HomeDomain", f"f{i}", b"x", 1) for i in range(100)})
    cikar.cikar(bk1, tmp_path / "ext1", b"dogru-sifre")
    bk2 = _yedek(tmp_path / "bk2", {f"{i:04x}": ("HomeDomain", f"f{i}", b"x", 1) for i in range(94)})
    cikar.cikar(bk2, tmp_path / "ext2", b"dogru-sifre")
    d = denetim.denetle(tmp_path / "ext2", tmp_path / "ext1")
    assert d["dur"] and "100 → 94" in d["dur"][0]
    bk3 = _yedek(tmp_path / "bk3", {f"{i:04x}": ("HomeDomain", f"f{i}", b"x", 1) for i in range(96)})
    cikar.cikar(bk3, tmp_path / "ext3", b"dogru-sifre")
    d = denetim.denetle(tmp_path / "ext3", tmp_path / "ext1")
    assert not d["dur"]
    assert any("dosyası yok" in u for u in d["uyari"])          # gerekli dosyalar sentetikte yok → uyarı


def test_denetim_quick_check_ve_son_kayit(tmp_path):
    ext = tmp_path / "ext"
    p = ext / "HomeDomain/Library/CallHistoryDB/CallHistory.storedata"
    p.parent.mkdir(parents=True)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE ZCALLRECORD (ZDATE REAL)")
    c.execute("INSERT INTO ZCALLRECORD VALUES (784900000)")      # 2025-11 civarı → 7 günden eski
    c.commit()
    c.close()
    d = denetim.denetle(ext, None)
    assert d["quick_check"]["CallHistory"] == "ok"
    assert d["son_kayit"]["CallHistory"].startswith("2025-")
    assert any(u.startswith("CallHistory: son kayıt") for u in d["uyari"])
    assert not list(p.parent.glob("*-shm"))                       # kaynağa dosya eklenmedi


def test_denetim_ozel_tokenizer_uyari_degil(tmp_path, monkeypatch):
    ext = tmp_path / "ext"
    p = ext / "HomeDomain/Library/AddressBook/AddressBook.sqlitedb"
    p.parent.mkdir(parents=True)
    sqlite3.connect(p).close()
    real = denetim.ac

    class Bag:
        def __init__(self, c):
            self.c = c

        def execute(self, q):
            raise sqlite3.OperationalError("unknown tokenizer: ab_cf_tokenizer")

    import contextlib

    @contextlib.contextmanager
    def sahte(db):
        with real(db) as c:
            yield Bag(c)
    monkeypatch.setattr(denetim, "ac", sahte)
    d = denetim.denetle(ext, None)
    assert d["quick_check"]["Rehber"].startswith("atlandı")
    assert not any("Rehber" in u for u in d["uyari"])
