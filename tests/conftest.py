"""Gerçek veri koruması (2026-11-13 kazası: test_akis'te monkeypatch.undo() CFV_DATA'yı geri aldı, test gerçek
processed/akis.sqlite'a yazdı). Kurallar:
  - Oturum başında CFV_DATA geçici bir dizine ayarlanır (CFV_DATA vermeyen test de gerçek veriye düşmesin).
  - Her test başında ve sonunda veri kökü denetlenir.
  - db.data_dir() (ve onu doğrudan import eden modüllerdeki kopyası) her çağrıda denetlenir: test ortasında
    gerçek veriye (/mnt/depo1/cfv/data ya da repo/data symlink'i) çözülürse yazmadan önce pytest DURUR.
"""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest

from cfv import db

_REAL_ROOTS = {Path(p).resolve() for p in ("/mnt/depo1/cfv/data", db.REPO_ROOT / "data")}
_orig_data_dir = db.data_dir
_DIRECT_IMPORTERS = ("cfv.gmail_sync", "cfv.gcal_sync")  # `from cfv.db import data_dir` yapanlar


def _is_real(p: Path) -> bool:
    r = Path(p).resolve()
    return any(r == root or r.is_relative_to(root) for root in _REAL_ROOTS)


def _check(where: str) -> Path:
    p = _orig_data_dir()
    if _is_real(p):
        pytest.exit(f"GERÇEK VERİ KORUMASI ({where}): veri kökü {p} → {Path(p).resolve()}; test durduruldu. "
                    "CFV_DATA'yı tmp_path'e ayarla, monkeypatch.undo() kullanma.", returncode=3)
    return p


def _guarded_data_dir() -> Path:
    return _check(os.environ.get("PYTEST_CURRENT_TEST", "data_dir()").split(" ")[0])


def pytest_configure(config):
    os.environ["CFV_DATA"] = tempfile.mkdtemp(prefix="cfv-test-data-")
    db.data_dir = _guarded_data_dir
    for name in _DIRECT_IMPORTERS:
        try:
            mod = importlib.import_module(name)
        except Exception:  # isteğe bağlı bağımlılık eksikse o modülün testleri zaten atlanır
            continue
        if getattr(mod, "data_dir", None) is _orig_data_dir:
            mod.data_dir = _guarded_data_dir


@pytest.fixture(autouse=True)
def _gercek_veri_korumasi(request):
    _check(f"{request.node.nodeid} başı")
    yield
    _check(f"{request.node.nodeid} sonu")
