"""Büyüme kaydı: data/logs/buyume.tsv — günde tek satır.

Sütunlar sabit: tarih, toplam, fark, kaynaklar, foto_turev, kisi
  kaynaklar  = "whatsapp=313172(+40) iphone-photos=28941(+12) ..."  (yeni kaynak çıkınca sütun değişmez;
               olay üretmeyen kaynaklar — rehber — burada yok, toplam olay sayısıdır)
  foto_turev = "vektor=25424(+12) small=... mid=... eval=..."
  kisi       = "rehber=826(+0) numara=816(+0) whatsapp_jid=... eposta=..." (person_alias; 2026-11-13'ten önce boş)
Aynı gün yeniden koşulursa (elle tekrar tur) bugünün satırı güncellenir; eski hâli buyume-degisen.tsv'ye eklenir
(türetilmiş kayıt, ama değer kaybolmasın). Yazım geçici dosya + rename ile.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from cfv import db

HEADER = "tarih\ttoplam\tfark\tkaynaklar\tfoto_turev\tkisi\n"
NO_EVENTS = ("contacts",)  # olay üretmez (kişi takma adları) → kaynaklar/toplamda değil, kisi sütununda
# person_alias.source_kind → kisi sütunundaki ad
ALIAS_NAMES = {"contacts": "rehber", "phone": "numara", "whatsapp": "whatsapp_jid", "email": "eposta"}
_KV = re.compile(r"([\w.-]+)=(-?\d+)")


def path() -> Path:
    return db.data_dir() / "logs" / "buyume.tsv"


def _parse(field: str) -> dict[str, int]:
    return {k: int(v) for k, v in _KV.findall(field)}


def rows(p: Path | None = None) -> list[dict]:
    p = p or path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 5:
            continue
        out.append({"tarih": f[0], "toplam": int(f[1]), "fark": f[2],
                    "kaynaklar": _parse(f[3]), "foto_turev": _parse(f[4]),
                    "kisi": _parse(f[5]) if len(f) > 5 else {}, "satir": line})
    return out


def _fmt(cur: dict[str, int], prev: dict[str, int] | None) -> str:
    parts = []
    for k in sorted(cur, key=lambda k: (-cur[k], k)):
        d = "" if prev is None else f"({cur[k] - prev.get(k, 0):+d})"
        parts.append(f"{k}={cur[k]}{d}")
    for k in sorted(set(prev or {}) - set(cur)):
        if not prev[k]:
            continue
        parts.append(f"{k}=0({-prev[k]:+d})")
    return " ".join(parts)


def kisi(alias_counts: dict[str, int]) -> dict[str, int]:
    """person_alias sayıları (source_kind → n) → kisi sütunu ({rehber, numara, ...})."""
    return {ALIAS_NAMES[k]: n for k, n in alias_counts.items() if k in ALIAS_NAMES}


def append(counts: dict[str, int], derivs: dict[str, int], day: dt.date | None = None,
           p: Path | None = None, people: dict[str, int] | None = None) -> str | None:
    """Bugünün satırını yaz: yoksa ekle, varsa (aynı gün yeniden tur) güncelle. Yazılan satırı döner;
    bugünün satırı zaten aynıysa None."""
    p = p or path()
    day = (day or dt.date.today()).isoformat()
    counts = {k: v for k, v in counts.items() if k not in NO_EVENTS}
    old = rows(p)
    replaced = old.pop() if old and old[-1]["tarih"] == day else None
    prev = old[-1] if old else None
    total = sum(counts.values())
    fark = "" if prev is None else f"{total - prev['toplam']:+d}"
    line = "\t".join([day, str(total), fark, _fmt(counts, prev and prev["kaynaklar"]),
                      _fmt(derivs, prev and prev["foto_turev"]),
                      _fmt(people or {}, (prev or {}).get("kisi") or None)]) + "\n"
    if replaced and replaced["satir"] + "\n" == line:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    if replaced:
        h = p.with_name("buyume-degisen.tsv")
        with open(h, "a") as f:
            if h.stat().st_size == 0:
                f.write("degisti_at\t" + HEADER)
            f.write(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\t" + replaced["satir"] + "\n")
    body = p.read_text().splitlines(keepends=True)[1:] if p.exists() else []  # eski satırlar birebir korunur
    if replaced:
        assert body and body[-1].rstrip("\n") == replaced["satir"]
        body.pop()
    tmp = p.with_suffix(".tsv.yeni")
    tmp.write_text(HEADER + "".join(body) + line)
    tmp.replace(p)
    return line


def table(n: int = 14, p: Path | None = None) -> str:
    rs = rows(p)
    if not rs:
        return "buyume.tsv henüz yok (ilk gece turundan sonra oluşur)."
    lines = [f"{'tarih':10}  {'toplam':>8}  {'fark':>6}  kaynak farkları"]
    for r in rs[-n:]:
        lines.append(f"{r['tarih']:10}  {r['toplam']:>8}  {r['fark'] or '-':>6}  "
                     + " ".join(f"{k}{v:+d}" for k, v in _diffs(rs, r).items() if v))
    last = rs[-1]
    for days in (7, 30):
        base = next((r for r in rs if r["tarih"] >= (dt.date.fromisoformat(last["tarih"])
                                                     - dt.timedelta(days=days)).isoformat()), None)
        if base and base is not last:
            lines.append(f"son {days} gün ({base['tarih']} → {last['tarih']}): {last['toplam'] - base['toplam']:+d} olay")
    return "\n".join(lines)


def _diffs(rs: list[dict], r: dict) -> dict[str, int]:
    i = rs.index(r)
    if i == 0:
        return {}
    prev = rs[i - 1]["kaynaklar"]
    return {k: r["kaynaklar"].get(k, 0) - prev.get(k, 0) for k in sorted(set(prev) | set(r["kaynaklar"]))}
