"""Gün/hafta/ay raporları. Raporları bp ajanı `cfv-editor` yazar (iş tanımı:
src/cfv/prompts/editor.md); bu modül yalnızca veriyi LLM'e sığacak parçalara böler:

    cfv day 2026-10-31               # o günün ham verisi (sığıyorsa tek parça)
    cfv day 2026-10-31 --plan        # sığmıyorsa parçalar: 3 saatlik, o da sığmazsa 1 saatlik
    cfv day 2026-10-31 --hours 9-12  # tek parça
    cfv day 2026-10-31 --skeleton    # gün iskeleti (takvim, ödev, sağlık, sayılar); parçalı günde
    cfv review send                  # eksik raporları bulup ajana tek mesajla iş listesi gönderir

Hiyerarşi: parça → gün → hafta → ay. Her seviye bir alttakinin raporlarını okur, ham veriyi değil;
böylece veri ne kadar büyürse büyüsün her adım tek bir LLM bağlamına sığar.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from cfv import db

EDITOR = "cfv-editor"
LIMIT = 60_000      # karakter (~20k token); bir dökümün üst sınırı
MAX_TEXT = 220

def days_dir() -> Path:
    return db.data_dir() / "processed" / "days"


def report_path(d: dt.date) -> Path:
    return days_dir() / str(d.year) / f"{d.isoformat()}.md"


def part_path(d: dt.date, h0: int, h1: int) -> Path:
    return db.data_dir() / "processed" / "parts" / str(d.year) / d.isoformat() / f"{h0:02d}-{h1:02d}.md"


def week_path(year: int, week: int) -> Path:
    return db.data_dir() / "processed" / "weeks" / str(year) / f"{year}-W{week:02d}.md"


def month_path(year: int, month: int) -> Path:
    return db.data_dir() / "processed" / "months" / str(year) / f"{year}-{month:02d}.md"


def _hm(start_utc: str, off: int | None) -> str:
    t = dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00")) + dt.timedelta(minutes=off or 180)
    return t.strftime("%H:%M")


def _cut(s: str | None, n: int = MAX_TEXT) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _local_hour(r) -> float:
    t = dt.datetime.fromisoformat(r[2].replace("Z", "+00:00")) + dt.timedelta(minutes=r[4] or 180)
    return t.hour + t.minute / 60


def dossier(conn, d: dt.date, hours: tuple[int, int] | None = None, skeleton: bool = False) -> str:
    """Bir günün verisi, LLM'e verilecek sade metin.
    hours=(9, 12): yalnız o saatlerdeki saatli kayıtlar (parça). skeleton: mesaj/foto/mail ayrıntısı
    yerine sayılar (parçalı günde gün ajanının girdisi)."""
    day = d.isoformat()
    rows = conn.execute("""SELECT id, kind, start_utc, end_utc, tz_offset_min, time_quality, title, body, payload
                           FROM event WHERE local_date = ? ORDER BY start_utc""", (day,)).fetchall()
    if hours:
        rows = [r for r in rows if r[5] not in ("day", "week") and hours[0] <= _local_hour(r) < hours[1]]
    by = defaultdict(list)
    for r in rows:
        by[r[1]].append(r)
    people = defaultdict(list)
    for eid, name, role in conn.execute("""
            SELECT ep.event_id, p.display_name, ep.role FROM event_person ep
            JOIN person p ON p.id = ep.person_id JOIN event e ON e.id = ep.event_id
            WHERE e.local_date = ?""", (day,)):
        people[eid].append(name)
    cal_photos = dict(conn.execute("""SELECT l.b, count(*) FROM event_link l JOIN event c ON c.id = l.b
                                      WHERE l.kind='photo_at' AND c.local_date = ? GROUP BY l.b""", (day,)))

    out = [f"# {day} ({['Pazartesi','Salı','Çarşamba','Perşembe','Cuma','Cumartesi','Pazar'][d.weekday()]})"
           + (f" saat {hours[0]:02d}-{hours[1]:02d}" if hours else " — iskelet" if skeleton else ""), ""]
    counts = Counter(r[1] for r in rows)
    out.append("Sayılar: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "veri yok"))
    out.append("")

    if by["calendar"]:
        out.append("## Takvim")
        for r in by["calendar"]:
            p = json.loads(r[8] or "{}")
            when = "tüm gün" if r[5] == "day" else f"{_hm(r[2], r[4])}-{_hm(r[3], r[4]) if r[3] else '?'}"
            extra = f" | yer: {p['location']}" if p.get("location") else ""
            extra += f" | o sırada {cal_photos[r[0]]} foto" if r[0] in cal_photos else ""
            out.append(f"- {when} {r[6] or '(başlıksız)'} [{p.get('calendar', '')}]{extra}")
        out.append("")

    todos = [] if hours else by["todo"] + conn.execute("""SELECT id, kind, start_utc, end_utc, tz_offset_min, time_quality, title,
            body, payload FROM event WHERE kind='todo' AND time_quality='week'
            AND local_date BETWEEN date(?, '-6 days') AND ? AND local_date != ?""", (day, day, day)).fetchall()
    if todos:
        out.append("## Ödev/yapılacaklar (ödev notundan; [x] yapıldı, [ ] açık; 'hafta' = günü belirsiz)")
        for r in todos:
            p = json.loads(r[8] or "{}")
            box = "[x]" if p.get("done") else "[ ]" if p.get("done") is False else "-"
            sec = f" ({p['section']})" if p.get("section") else ""
            out.append(f"- {box} {'hafta: ' if r[5] == 'week' else ''}{r[6]}{sec}")
        out.append("")

    for kind, title in (("note", "Notlar (bu gün oluşturulan)"),):
        if by[kind]:
            out.append(f"## {title}")
            for r in by[kind]:
                out.append(f"- {r[6]}: {_cut(r[7], 600)}")
            out.append("")

    if skeleton:
        n = {k: len(v) for k, v in by.items() if k in {"photo", "video", "email", "whatsapp"} and v}
        if n:
            blocks = Counter(f"{int(_local_hour(r)) // 3 * 3:02d}" for k in n for r in by[k])
            out += ["## Ayrıntısı parça raporlarında olanlar",
                    "- " + ", ".join(f"{k}={v}" for k, v in n.items()),
                    "- 3 saatlik bloklara göre: " + ", ".join(f"{h}:00×{c}" for h, c in sorted(blocks.items())), ""]
        for k in ("photo", "video", "email", "whatsapp"):
            by.pop(k, None)

    media = by["photo"] + by["video"]
    if media:
        out.append(f"## Fotoğraf/video ({len(by['photo'])} foto, {len(by['video'])} video, "
                   f"{_hm(media[0][2], media[0][4])}–{_hm(media[-1][2], media[-1][4])})")
        hours = Counter(_hm(r[2], r[4])[:2] for r in media)
        out.append("Saatlere göre: " + ", ".join(f"{h}:00×{n}" for h, n in sorted(hours.items())))
        ppl = Counter(n for r in media for n in people[r[0]])
        if ppl:
            out.append("Fotoğraflardaki kişiler: " + ", ".join(f"{n}×{c}" for n, c in ppl.most_common(15)))
        places = Counter()
        for r in media:
            p = json.loads(r[8] or "{}")
            m = p.get("moment") or {}
            for x in (m.get("title"), m.get("subtitle")) if isinstance(m, dict) else (m,):
                if x:
                    places[x] += 1
        if places:
            out.append("Yerler: " + ", ".join(f"{n}×{c}" for n, c in places.most_common(8)))
        desc = [r[7] for r in media if r[7] and "Açıklama" in (r[7] or "")]
        if desc:
            out.append("Açıklamalar: " + " | ".join(_cut(x, 120) for x in desc[:10]))
        out.append("")

    if by["email"]:
        out.append(f"## E-posta ({len(by['email'])})")
        for r in by["email"][:60]:
            p = json.loads(r[8] or "{}")
            arrow = "→" if p.get("direction") == "sent" else "←"
            out.append(f"- {_hm(r[2], r[4])} {arrow} {_cut(p.get('from') if arrow == '←' else p.get('to'), 60)}: {_cut(r[6], 120)}")
        out.append("")

    if by["whatsapp"]:
        chats = defaultdict(list)
        for r in by["whatsapp"]:
            p = json.loads(r[8] or "{}")
            chats[(p.get("chat") or r[6] or "?", p.get("chat_type"))].append((r, p))
        out.append(f"## WhatsApp ({len(by['whatsapp'])} mesaj, {len(chats)} sohbet)")
        for (chat, ctype), msgs in sorted(chats.items(), key=lambda kv: -len(kv[1])):
            mine = sum(1 for _, p in msgs if p.get("from_me"))
            out.append(f"### {chat} ({'grup' if ctype == 'group' else 'birebir' if ctype == 'dm' else ctype}; "
                       f"{len(msgs)} mesaj, {mine} benden)")
            for r, p in msgs:
                who = "ben" if p.get("from_me") else (p.get("sender") or chat)
                out.append(f"  {_hm(r[2], r[4])} {who}: {_cut(r[7])}")
        out.append("")

    health = []
    for r in [] if hours else by["zepp_day"]:
        p = json.loads(r[8] or "{}")
        health.append(f"adım {p.get('steps')}, dinlenme nabzı {p.get('rest_hr')}, PAI {p.get('pai')}")
    for r in by["sleep"]:
        p = json.loads(r[8] or "{}")
        health.append(f"uyku {round((p.get('asleep_min') or 0) / 60, 1)} sa ({_hm(r[2], r[4])}→{_hm(r[3], r[4]) if r[3] else '?'})")
    for r in by["workout"]:
        health.append(f"egzersiz: {r[6]} {_hm(r[2], r[4])}")
    if health:
        out += ["## Sağlık (sayılar)", *[f"- {h}" for h in health], ""]

    others = [k for k in by if k not in {"calendar", "todo", "note", "photo", "video", "email", "whatsapp",
                                         "zepp_day", "sleep", "workout", "day_report"}]
    for k in others:
        out.append(f"## {k} ({len(by[k])})")
        out += [f"- {_hm(r[2], r[4])} {_cut(r[6], 120)}" for r in by[k][:50]]
        out.append("")
    text = "\n".join(out)
    if len(text) > LIMIT:  # 1 saatlik parça bile sığmıyorsa: son çare kes
        text = text[:LIMIT] + f"\n\n… (sınır: {len(text) - LIMIT} karakter kesildi)"
    return text


def plan(conn, d: dt.date) -> list[tuple[int, int]]:
    """Gün sığıyorsa [] (tek parça). Sığmıyorsa veri olan 3 saatlik bloklar; bir blok da
    sığmıyorsa o blok 1 saatlik parçalara bölünür."""
    if len(dossier(conn, d)) < LIMIT:
        return []
    out = []
    for h in range(0, 24, 3):
        text = dossier(conn, d, (h, h + 3))
        if "Sayılar: veri yok" in text:
            continue
        if len(text) < LIMIT:
            out.append((h, h + 3))
        else:
            out += [(x, x + 1) for x in range(h, h + 3) if "Sayılar: veri yok" not in dossier(conn, d, (x, x + 1))]
    return out


def jobs(today: dt.date | None = None) -> list[str]:
    """Eksik raporlar: geçen ayın başından dünkü güne kadar günler (sığmayan günün önce parçaları),
    biten son hafta, biten son ay. Alt seviye önce gelir; üst seviye alttakinin raporlarını kullanır."""
    today = today or dt.date.today()
    last_sunday = today - dt.timedelta(days=today.weekday() + 1)
    last_month_end = today.replace(day=1) - dt.timedelta(days=1)
    start = min(last_month_end.replace(day=1), last_sunday - dt.timedelta(days=6))
    conn = db.connect(db.default_db_path())
    out = []
    for d in (start + dt.timedelta(days=i) for i in range((today - start).days)):
        if report_path(d).exists():
            continue
        parts = plan(conn, d)
        out += [f"parça {d.isoformat()} {h0:02d}-{h1:02d}" for h0, h1 in parts
                if not part_path(d, h0, h1).exists()]
        out.append(f"gün {d.isoformat()}" + (" (parçalı)" if parts else ""))
    conn.close()
    y, w, _ = last_sunday.isocalendar()
    if not week_path(y, w).exists():
        out.append(f"hafta {y}-W{w:02d}")
    if not month_path(last_month_end.year, last_month_end.month).exists():
        out.append(f"ay {last_month_end.year}-{last_month_end.month:02d}")
    return out


def send(today: dt.date | None = None) -> str:
    todo = jobs(today)
    if not todo:
        return "eksik rapor yok"
    msg = ("İş listesi (src/cfv/prompts/editor.md'yi oku, sırayla yap): " + "; ".join(todo))
    p = subprocess.run(["bp", "msg", EDITOR, msg], capture_output=True, text=True)
    return (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr) else msg
