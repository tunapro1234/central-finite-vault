"""idevicebackup2 sürücüsü: sahte program ile şifre bekleme zaman aşımı ve başarı ölçütü."""

import plistlib
import stat

import pytest

from cfv_iphone import yedek

UDID = "00000000-0000000000000000"


@pytest.fixture(autouse=True)
def _veri(monkeypatch, tmp_path):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))


def _program(tmp_path, govde: str) -> str:
    p = tmp_path / "sahte-idevicebackup2"
    p.write_text("#!/bin/sh\n" + govde)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_sifre_girilmezse_kapatir_ve_bildirir(tmp_path):
    prog = _program(tmp_path, 'echo "Requesting backup from device..."\nsleep 30\n')
    bildirim = []
    s = yedek.calistir(UDID, program=prog, sifre_bekleme=1, bildirim=lambda *a: bildirim.append(a),
                       logla=lambda m: None)
    assert not s.ok and s.neden == "şifre girilmedi"
    assert bildirim and bildirim[0][2] == "critical"


def test_basari_olcutu(tmp_path, monkeypatch):
    hedef = tmp_path / "data/work/iphone-yedek" / UDID
    prog = _program(tmp_path, f'''echo "Requesting backup from device..."
echo "Incremental backup mode."
printf "[=====     ]  50%% Finished\\r[==========] 100%% Finished\\n"
mkdir -p "{hedef}"
echo "Backup Successful."
''')
    hedef.parent.mkdir(parents=True)
    s = yedek.calistir(UDID, program=prog, bildirim=lambda *a: None, logla=lambda m: None)
    assert not s.ok and "Status.plist" in s.neden                # dosyalar yoksa başarı sayılmaz
    (hedef / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": "finished"}))
    (hedef / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    s = yedek.calistir(UDID, program=prog, bildirim=lambda *a: None, logla=lambda m: None)
    assert not s.ok and "şifreli değil" in s.neden
    (hedef / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
    s = yedek.calistir(UDID, program=prog, bildirim=lambda *a: None, logla=lambda m: None)
    assert s.ok and s.neden == "tamam" and s.tam_yedek is False


def test_rc_sifir_degilse_basarisiz(tmp_path):
    prog = _program(tmp_path, 'echo "Backup Failed (Error Code -4)."\nexit 1\n')
    s = yedek.calistir(UDID, program=prog, bildirim=lambda *a: None, logla=lambda m: None)
    assert not s.ok and "rc=1" in s.neden
