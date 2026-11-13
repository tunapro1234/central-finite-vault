"""WhatsApp köprüsü (/srv/whatsapp/messages.jsonl) → 'whatsapp' olayları, iPhone yedekleri arasındaki günler için.

İki adım:
  topla  (gece turu, ingest'ten önce): köprü store'unu SALT-OKUR, inode+offset ile artımlı okur; yeni satırları
         data/raw/whatsapp-kopru/<YYYY-MM-DD>.jsonl'e EKLER (raw yalnız eklenir; mesaj id'si zaten raw'daysa atlanır,
         okuma durumu kaybolsa da çift kopya olmaz). Rüya grubu kopyalanmaz (cfv.ruya.is_dream_chat; metni yalnız
         dream'de). deleted.jsonl / calls.jsonl / auth/ OKUNMAZ; Baileys bağlantısı ya da wa komutu yok.
  ingest (`cfv ingest whatsapp-kopru`, SOURCES'ta whatsapp'tan sonra): raw/whatsapp-kopru/*.jsonl'i okur.
         Atlanan: stanza id'si iPhone 'whatsapp' kaynağında olan (iPhone zengin: medya, alıntı, tepki);
         iPhone yedeğinden ESKİ olup iPhone'da olmayan (telefonda silinmiş sayılır — telefon esas);
         Rüya grubu; içeriği yazılmamış giden ('[icerik kaydedilmedi]', routes.icerik_yazma).
         Agent'ların Tuna hesabından attığı mesajlar payload.agent=<ad> ile işaretlenir.
Yeni iPhone yedeği gelince o güne kadarki köprü mesajları whatsapp-kopru'dan whatsapp'a geçer (azalma denetimi
bunu bilir: akis.gunluk.drops).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

from cfv import db
from cfv.ingest import Batch
from cfv.ruya import is_dream_chat

IMPORTER = "cfv.sources.whatsapp_kopru@1"
KIND = "whatsapp-kopru"
STORE = Path(os.environ.get("CFV_WA_STORE", "/srv/whatsapp/messages.jsonl"))
HIDDEN = "[icerik kaydedilmedi]"
AGENT_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")      # bp agent adı biçimi (server-main, cfv-akis ...)
AGENT_PREFIX = re.compile(r"\[([a-z0-9]+(?:-[a-z0-9]+)*)\]")
MEDIA = {"-audio": "[ses]", "-image": "[foto]", "-video": "[video]", "-sticker": "[çıkartma]",
         "-document": "[belge]"}


def raw_dir() -> Path:
    return db.data_dir() / "raw" / "whatsapp-kopru"


def state_path() -> Path:
    return db.data_dir() / "processed" / "whatsapp-kopru" / "okuma.json"


def _key(rec: dict, line: str) -> str:
    return rec.get("id") or "sha1:" + hashlib.sha1(line.encode()).hexdigest()


def _is_dream(rec: dict) -> bool:
    return is_dream_chat(rec.get("chatJid"), rec.get("chatName"), rec.get("chatType") == "group")


def _raw_lines(d: Path):
    for f in sorted(d.glob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    yield f, line


# ---- 1. topla -----------------------------------------------------------------------------------------------
def collect(store: Path | None = None, out: Path | None = None, *, today: dt.date | None = None,
            dry_run: bool = False) -> dict:
    """Köprü store'undaki yeni satırları raw'a ekle. dry_run: yalnız say, hiçbir şey yazma."""
    store, out = store or STORE, out or raw_dir()
    today = today or dt.date.today()
    st_path = state_path() if out == raw_dir() else out.parent / f".{out.name}-okuma.json"
    pos = json.loads(st_path.read_text()) if st_path.exists() else {}
    seen = set()
    for _, line in _raw_lines(out):
        try:
            seen.add(_key(json.loads(line), line))
        except ValueError:
            continue
    stats = {"okunan": 0, "yeni": 0, "zaten_var": 0, "atlanan_ruya": 0, "bozuk": 0}
    new_lines: list[str] = []
    new_pos = dict(pos)
    for f in sorted(store.parent.glob(store.name + "*")):     # döndürme gelirse messages.jsonl.1 ...
        if f.suffix == ".gz" or not f.is_file():
            continue
        st, old = f.stat(), pos.get(f.name, {})
        off = old.get("offset", 0) if old.get("inode") == st.st_ino and old.get("offset", 0) <= st.st_size else 0
        with f.open("rb") as fh:
            fh.seek(off)
            chunk = fh.read()
        done = chunk.rfind(b"\n") + 1                           # yarım yazılmış son satır sonraki tura
        new_pos[f.name] = {"inode": st.st_ino, "offset": off + done}
        for line in chunk[:done].decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            stats["okunan"] += 1
            try:
                rec = json.loads(line)
            except ValueError:
                stats["bozuk"] += 1
                continue
            if _is_dream(rec):
                stats["atlanan_ruya"] += 1
                continue
            k = _key(rec, line)
            if k in seen:
                stats["zaten_var"] += 1
                continue
            seen.add(k)
            new_lines.append(line)
    stats["yeni"] = len(new_lines)
    if dry_run:
        return stats
    if new_lines:
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{today.isoformat()}.jsonl"
        with target.open("a", encoding="utf-8") as fh:            # yalnız ekleme
            fh.write("".join(line + "\n" for line in new_lines))
            fh.flush()
            os.fsync(fh.fileno())
        stats["dosya"] = target.name
    st_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = st_path.with_name(st_path.name + ".yeni")
    tmp.write_text(json.dumps(new_pos))
    tmp.replace(st_path)
    return stats


# ---- 2. ingest ----------------------------------------------------------------------------------------------
def _iphone(conn) -> tuple[set[str], dt.datetime | None]:
    """iPhone whatsapp kaynağındaki stanza id'leri + yedek tarihi (en yeni snapshot günü, UTC gece yarısı)."""
    ids = {x.rpartition("/")[2].split("#")[0] for (x,) in conn.execute(
        "SELECT e.external_id FROM event e JOIN source s ON s.id=e.source_id WHERE s.kind='whatsapp'")}
    snap = conn.execute("SELECT max(s.snapshot) FROM source s WHERE s.kind='whatsapp' AND EXISTS "
                        "(SELECT 1 FROM event e WHERE e.source_id=s.id)").fetchone()[0]
    cut = dt.datetime.fromisoformat(snap[:10]).replace(tzinfo=dt.timezone.utc) if snap else None
    return ids, cut


def agent_of(rec: dict, known: set[str] | frozenset = frozenset()) -> str | None:
    """Tuna'nın hesabından agent'ın attığı mesaj mı? Köprünün giden kaydında senderName = agent adı;
    telefondan dönen yankıda metin '[<agent>] ' ile başlar — yalnız köprü kayıtlarında görülmüş bir agent adıysa
    (Tuna'nın kendi yazdığı "[not] ..." agent sayılmasın)."""
    if not rec.get("fromMe"):
        return None
    sn = rec.get("senderName") or ""
    if AGENT_NAME.fullmatch(sn):
        return sn
    m = AGENT_PREFIX.match(rec.get("text") or "")
    return m.group(1) if m and m.group(1) in known else None


def _body(rec: dict) -> tuple[str, str]:
    text, f = rec.get("text") or "", rec.get("file")
    if not f:
        return text, "text"
    stem = Path(f).stem
    ph = next((v for k, v in MEDIA.items() if stem.endswith(k)), "[dosya]")
    if not text or Path(text).name == Path(f).name:
        return ph, ph.strip("[]")
    return f"{ph} {text}", ph.strip("[]")


def ingest(conn, raw: Path | None = None) -> dict:
    raw = raw or raw_dir()
    if not raw.is_dir() or not any(raw.glob("*.jsonl")):
        raise FileNotFoundError(raw)
    ids, cut = _iphone(conn)
    stats = {"satir": 0, "mesaj": 0, "iphone_da_var": 0, "iphone_yedeginden_eski": 0, "ruya": 0,
             "icerik_yok": 0, "cift": 0, "agent": 0, "bozuk": 0}
    last = None
    recs = []
    seen: set[str] = set()
    for _, line in _raw_lines(raw):
        stats["satir"] += 1
        try:
            rec = json.loads(line)
            t = dt.datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
        except (ValueError, KeyError, AttributeError):
            stats["bozuk"] += 1
            continue
        k = _key(rec, line)
        if k in seen:
            stats["cift"] += 1
            continue
        seen.add(k)
        if rec.get("id") in ids:
            stats["iphone_da_var"] += 1
        elif cut and t < cut:
            stats["iphone_yedeginden_eski"] += 1
        elif _is_dream(rec):
            stats["ruya"] += 1
        elif rec.get("text") == HIDDEN:
            stats["icerik_yok"] += 1
        else:
            recs.append((t, k, rec))
    known = {r.get("senderName") for _, _, r in recs
             if r.get("fromMe") and AGENT_NAME.fullmatch(r.get("senderName") or "")}
    batch = Batch(conn, KIND, raw, IMPORTER)
    for t, k, rec in sorted(recs, key=lambda x: x[0]):
        chat_jid, ctype = rec.get("chatJid") or "", rec.get("chatType") or "other"
        name = rec.get("chatName")
        chat = name if name and name != "(giden)" else chat_jid
        from_me = bool(rec.get("fromMe"))
        agent = agent_of(rec, known)
        sender = (agent or "ben") if from_me else (rec.get("senderName") or rec.get("senderJid"))
        body, mtype = _body(rec)
        payload = {"chat": chat, "chat_type": ctype, "chat_jid": chat_jid, "from_me": from_me, "sender": sender,
                   "type": mtype, "kaynak": "kopru"}
        if agent:
            payload["agent"] = agent
            stats["agent"] += 1
        if rec.get("file"):
            payload["file"] = rec["file"]
        eid = batch.event(f"{chat_jid}/{k}", "whatsapp", t, title=chat, body=body, payload=payload)
        stats["mesaj"] += 1
        last = t if last is None or t > last else last
        # karşı taraf: DM'de sohbet kişisi, grupta mesajı atan (iPhone okuyucusuyla aynı alias türü: 'whatsapp')
        if ctype == "dm" and chat_jid and chat != chat_jid:
            batch.link_person(eid, batch.person(chat_jid, chat, "whatsapp"), "counterpart")
        elif ctype == "group" and not from_me and rec.get("senderJid") and rec.get("senderName"):
            batch.link_person(eid, batch.person(rec["senderJid"], rec["senderName"], "whatsapp"), "sender")
    if last:
        conn.execute("UPDATE source SET snapshot=? WHERE id=?",
                     (last.strftime("%Y-%m-%dT%H:%M:%SZ"), batch.source_id))
    stats["iphone_kesim"] = cut.date().isoformat() if cut else None
    return stats


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] != ["topla"]:
        sys.exit("kullanım: python -m cfv.sources.whatsapp_kopru topla [--dry-run]")
    print(json.dumps(collect(dry_run="--dry-run" in sys.argv), ensure_ascii=False))
