#!/usr/bin/env python3
"""Meta/Instagram "bilgilerin indirilmeye hazır" mailini fark et → Tuna'ya laptop-notify + bp msg.

cfv-gmail-sync biriminin ExecStartPost='u (günlük gmail çekiminden hemen sonra). Yalnız okur:
data/raw/gmail/<hesap>/messages.jsonl (hesaplar data/config/gmail-accounts). Son 4 gündeki
(bağlantı birkaç gün geçerli) eşleşen her mail için BİR KEZ bildirir; gönderilen mail kimlikleri
data/logs/meta-watch.json'da. İlk çalıştırmada (durum dosyası yok) mevcut mailler bildirilmeden
"görüldü" sayılır. Mesaja mailin konusu/içeriği/bağlantısı girmez; yalnız hesap ve saat.
Hata olursa data/logs/meta-watch.log'a yazar, her durumda 0 ile çıkar.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DATA = Path(os.environ.get("CFV_DATA", Path(__file__).resolve().parents[2] / "data"))
WINDOW = dt.timedelta(days=4)
SENDER = re.compile(r"@(mail\.)?instagram\.com|@facebookmail\.com|@(mail\.)?meta\.com", re.I)
READY = re.compile(r"download is ready|information is ready|ready to download|indirilmeye hazır"
                   r"|indirmeye hazır|bilgilerin hazır", re.I)

log = logging.getLogger("cfv.meta_watch")


def is_ready_mail(m: dict) -> bool:
    return bool(SENDER.search(m.get("from") or "") and READY.search(m.get("subject") or ""))


def accounts() -> list[str]:
    p = DATA / "config" / "gmail-accounts"
    return [a.strip() for a in p.read_text().splitlines() if a.strip()] if p.exists() else []


def find(now: dt.datetime) -> list[tuple[str, dict]]:
    out = []
    for a in accounts():
        p = DATA / "raw" / "gmail" / a / "messages.jsonl"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if not line:
                continue
            m = json.loads(line)
            t = dt.datetime.fromisoformat(m["time"].replace("Z", "+00:00"))
            if now - t <= WINDOW and is_ready_mail(m):
                out.append((a, m))
    return out


def notify(account: str, m: dict) -> bool:
    short = account.split("@")[0]
    when = m["time"][:16].replace("T", " ") + "Z"
    ok_l = subprocess.run(["laptop-notify", "Instagram verisi hazır",
                           f"{short} hesabına Meta indirme maili geldi, bağlantı birkaç gün geçerli",
                           "normal"], check=False, capture_output=True).returncode == 0
    ok_b = subprocess.run(["bp", "msg", "server-cfv",
                           f"Instagram verisi hazır: {short} hesabına Meta indirme maili geldi ({when}); "
                           f"bağlantı birkaç gün geçerli. Tuna'ya laptop-notify "
                           f"{'gitti' if ok_l else 'GİTMEDİ'}."],
                          check=False, capture_output=True).returncode == 0
    if not ok_b:
        subprocess.run(["logger", "-t", "cfv-meta-watch", f"Instagram verisi hazır ({short}, {when})"],
                       check=False)
    return ok_l or ok_b


def run(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    state_p = DATA / "logs" / "meta-watch.json"
    first = not state_p.exists()
    seen = set(json.loads(state_p.read_text()).get("seen", [])) if not first else set()
    hits = find(now)
    sent = []
    for a, m in hits:
        if m["id"] in seen:
            continue
        if first or notify(a, m):
            seen.add(m["id"])
            if not first:
                sent.append(m["id"])
    state_p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_p.parent, prefix=".meta-watch.")
    with os.fdopen(fd, "w") as f:
        json.dump({"seen": sorted(seen), "last_run": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, f, indent=1)
    os.replace(tmp, state_p)
    return {"first": first, "matches": len(hits), "sent": sent}


def main() -> int:
    os.umask(0o077)
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA / "logs" / "meta-watch.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        r = run()
        log.info("eşleşen %d, bildirilen %d%s", r["matches"], len(r["sent"]),
                 " (ilk çalıştırma: bildirim yok)" if r["first"] else "")
    except Exception as e:  # gmail senkronunu bozmasın
        log.exception("hata: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
