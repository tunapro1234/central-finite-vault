# CFV — Central Finite Vault

Tuna'nın kişisel hayat verisi kasası. Fotolar, yüz/kişi etiketleri, iCloud Notlar,
Apple Music dinleme geçmişi, takvim, BeReal, Snapchat, YouTube geçmişi… hepsi **tek bir
zaman çizelgesinde** (SQLite) birleşir; Claude bu çizelgeyi bir **MCP sunucusu** üzerinden
sorgular.

**Her şey yerel.** Hiçbir veri dış servise gönderilmez; yalnızca Tuna'nın seçtiği
fotoğraflar/özetler sohbet sırasında Claude'a gösterilir.

İlk kullanım senaryosu: **Instagram postu hazırlamak** — "2024 yazı" gibi bir dönem
ver → o dönemin fotoğrafları (kimler var), o dönem çok dinlenen müzikler, takvimdeki
olaylar ve notlar tek yanıtta gelsin.

## Mimari

```
            data/raw/  (DOKUNULMAZ — kaynakların ham hali)
   ┌───────────┬──────────┬─────────┬──────────┬─────────┬─────────┐
   Photos.sqlite  iPhone yedeği  Apple Music  Snapchat   BeReal   Takeout
   (yüz/kişi)    (Notlar,Takvim) Play Activity  My Data    GDPR    (YouTube)
        │            │             │            │          │         │
        └────────────┴─── src/cfv/ingest/<kaynak>.py ──────┴─────────┘
                       (salt-okur aç → normalize et → idempotent yaz)
                                      │
                                      ▼
                     data/processed/cfv.sqlite   ← tek zaman çizelgesi
                     event · media · person · track · place · tag · FTS5
                                      │
                    ┌─────────────────┴──────────────────┐
                    ▼                                    ▼
             src/cfv/query.py                    src/cfv/mcp/ (salt-okur)
        (dönem özeti, kişi, arama)         Claude araçları: timeline, period_digest,
                                           search, photos_with, top_tracks ...
```

İlkeler:

1. **Ham veri değişmez.** `data/raw/` yalnızca okunur (SQLite'lar `mode=ro`/`immutable=1`
   ile açılır). Ham kaynaklar photos ajanı tarafından doldurulur.
2. **İşlenmiş DB atılabilir.** `data/processed/cfv.sqlite` her an `data/raw`'dan baştan
   üretilebilir. Import idempotent: `(source, external_id)` benzersiz.
3. **Medya kopyalanmaz.** Fotoğraf/video dosyaları yerinde kalır
   (`~/Pictures/icloud-sync/YYYY/AA`); DB yalnızca yol + hash + metadata tutar.
4. **Kod ve veri ayrı.** `src/` ve `tests/` içinde kişisel veri yok — fixture'lar
   sentetiktir. `data/` git dışında (`.gitignore`), `chmod 700`, ACL'siz.
5. **Zaman UTC.** Tüm zamanlar ISO-8601 UTC + ayrı `tz_offset_min`. Apple Core Data
   zamanları (2001-01-01 epoch) import sırasında dönüştürülür.

Ayrıntılar:

- [docs/data-manifest.md](docs/data-manifest.md) — her kaynak: nerede, format, durum
- [docs/schema.md](docs/schema.md) — ortak şema taslağı ve kaynak → event eşlemesi
- [docs/plan.md](docs/plan.md) — yol haritası

## Dizin yapısı

```
src/cfv/
  schema.sql        ortak şema (event merkezli) + Türkçe FTS5
  ingest.py         ortak import katmanı (salt-okur açma, idempotent yazma)
  sources/          kaynak okuyucular: photos, notes (+ödevler), calendar_ios, calendar_ics,
                    whatsapp, zepp
  search.py         temel arama (FTS + haftalara toplama, dönem özeti)
  tree.py           tarih bazlı klasör ağacı
  mcp_server.py     Claude için salt-okur MCP
  gcal_sync.py      Google Takvim → data/raw/google-calendar/ (gog, salt-okur, artımlı)
src/systemd/        cfv-* systemd --user birimleri (install.sh ile bağlanır)
tests/              testler + sentetik fixture'lar
docs/               belgeler
data/               KİŞİSEL VERİ — git dışı, chmod 700
  raw/              ham kaynaklar (photos ajanı doldurur; dokunma — tek istisna google-calendar/, cfv senkronu)
  processed/        cfv.sqlite + timeline/ (tamamen türetilmiş, her an yeniden üretilir)
  logs/             zamanlanmış işlerin log'ları (gcal-sync.log)
```

## Kullanım

```sh
uv sync
uv run cfv ingest all                 # tüm kaynakları içe aktar (~30 sn); tek tek: ingest photos notes ...
uv run cfv tree                       # data/processed/timeline/ klasör ağacını üret
uv run cfv search "taşınma" --by-week # "ne zaman?" → haftalara göre
uv run cfv search "fatura" --kind todo
uv run cfv sync gcal [--status]       # Google Takvim senkronu (timer bunu 30 dk'da bir çalıştırır)
uv run cfv chart                      # data/processed/chart.html — değişkenleri aç/kapa grafik
uv run cfv day 2026-10-31             # bir günün ham dosyası (gün ajanına giden metin)
uv run cfv days run [--from --to]     # gün raporları → data/processed/days/YYYY/YYYY-MM-DD.md
uv run pytest
```

### Klasör ağacı

`data/processed/timeline/2025/09-Eylül/W38 (15-21)/`
- `README.md` — gün gün: takvim, ödevler (tikli), notlar, Zepp (adım/uyku/egzersiz),
  fotoğraf sayısı + kişiler + yer, WhatsApp özeti
- `whatsapp.md` — haftanın mesajları (gün > sohbet)
- `notlar/` — o hafta oluşturulan notlar (markdown)
- `fotolar/` — `~/Pictures/icloud-sync` dosyalarına **sembolik link** (kopya yok)

Ay sınırındaki haftalar iki ayda da görünür (her birinde o ayın günleri).
Ağaç `cfv tree` ile baştan üretilir; elle yapılan değişiklikler kaybolur.

### Gün raporları

Raporları bp ajanı `cfv-editor` yazar (iş tanımı: `src/cfv/prompts/editor.md`, gün formatı:
`src/cfv/prompts/day.md`). Her sabah 05:00'te `cfv-day-report.timer` → `cfv review send`
eksikleri bulur (geçen ayın başından düne kadar günler, biten son hafta, biten son ay) ve
ajana tek mesajla iş listesi yollar. Ajan her gün için `cfv day YYYY-MM-DD` çıktısını
(takvim + o sırada çekilen foto sayısı, ödevler, notlar, fotolar, WhatsApp, mail, sağlık),
önceki günlerin raporlarını ve kişisel bağlamı (`data/config/day-context.md`, git dışı) okur.

Hiyerarşi **parça → gün → hafta → ay**; her adım bir alttakinin raporlarını okur, böylece veri
büyüse de her iş tek bir LLM bağlamına sığar. Bir günün dökümü sınırı (`days.LIMIT`, ~60 bin
karakter) aşarsa gün 3 saatlik parçalara, bir parça da aşarsa 1 saatliklere bölünür
(`cfv day <tarih> --plan`). Her parçayı ayrı bir alt ajan özetler; gün ajanı parça özetlerini ve
günün iskeletini (`--skeleton`: takvim, ödevler, sağlık, sayılar) birleştirir.

- parça: `data/processed/parts/YYYY/YYYY-MM-DD/HH-HH.md`
- gün: `data/processed/days/YYYY/YYYY-MM-DD.md` (YAML frontmatter + neler oldu, somut
  çıktılar, plan/gerçekleşen, açık işler, bağlantılar, değerlendirme, dönem sinyalleri)
- hafta: `data/processed/weeks/YYYY/YYYY-Www.md` (o haftanın gün raporlarından)
- ay: `data/processed/months/YYYY/YYYY-MM.md` (gün + hafta raporlarından)

Gün raporları `day_report` olarak aranabilir ve haftalık README'lerden linklidir.
Dönemler (era) ileride ay/hafta raporlarından çıkarılacak.

**Not:** gün raporu o günün mesajlarını Claude'a gönderir (Tuna'nın kararı, 2026-11-01).

### Eşleştirme

`cfv.link` takvim olaylarıyla fotoları eşleştirir (`event_link`, `photo_at`): saatli olayın
süresi içinde ±30 dk çekilen fotolar ve 2-14 günlük tüm-gün olaylarının (gezi, kamp) günleri.

### MCP

Repo kökündeki `.mcp.json` `cfv` sunucusunu tanımlar (bu klasörde açılan Claude oturumları
görür). Araçlar: `when`, `search`, `timeline`, `digest`, `week_page`, `people`, `photos`.
Sunucu veritabanını salt-okur açar.

`CFV_DATA` veri kökünü, `CFV_PHOTO_LIBRARY` foto kütüphanesini değiştirir.
