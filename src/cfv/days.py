"""Bir günün (ya da gün içi saat aralığının) verisini LLM'e verilecek sade metne döker.
Hiyerarşik raporlar bunu kullanır: bkz. cfv.reports.

    cfv day 2026-10-31               # o günün ham verisi
    cfv day 2026-10-31 --hours 9-12  # yalnız o saatler
    cfv day 2026-10-31 --skeleton    # saatsiz kayıtlar + sayılar (bölünmüş gün için)
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict

MAX_TEXT = 220


def _hm(start_utc: str, off: int | None) -> str:
    t = dt.datetime.fromisoformat(start_utc.replace("Z", "+00:00")) + dt.timedelta(minutes=off or 180)
    return t.strftime("%H:%M")


def _cut(s: str | None, n: int = MAX_TEXT) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _local_hour(r) -> float:
    t = dt.datetime.fromisoformat(r[2].replace("Z", "+00:00")) + dt.timedelta(minutes=r[4] or 180)
    return t.hour + t.minute / 60


def dossier(conn, d: dt.date, hours: tuple[int, int] | None = None, skeleton: bool = False,
            week_todos: bool = True) -> str:
    """Bir günün verisi, LLM'e verilecek sade metin.
    hours=(9, 12): yalnız o saatlerdeki saatli kayıtlar (parça). skeleton: mesaj/foto/mail ayrıntısı
    yerine sayılar (parçalı günde gün ajanının girdisi)."""
    day = d.isoformat()
    rows = conn.execute("""SELECT id, kind, start_utc, end_utc, tz_offset_min, time_quality, title, body, payload
                           FROM event WHERE local_date = ? AND kind NOT IN ('day_report', 'report')
                           ORDER BY start_utc""", (day,)).fetchall()
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

    todos = [] if hours else by["todo"] if not week_todos else by["todo"] + conn.execute("""SELECT id, kind, start_utc, end_utc, tz_offset_min, time_quality, title,
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
            out += ["## Ayrıntısı alt raporlarda olanlar",
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
                                         "zepp_day", "sleep", "workout"}]
    for k in others:
        out.append(f"## {k} ({len(by[k])})")
        out += [f"- {_hm(r[2], r[4])} {_cut(r[6], 120)}" for r in by[k][:50]]
        out.append("")
    return "\n".join(out)
