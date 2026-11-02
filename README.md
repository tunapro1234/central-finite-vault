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
uv run cfv sync gmail                 # Gmail senkronu (başlık + snippet)
uv run cfv chart                      # data/processed/chart.html — değişkenleri aç/kapa grafik
./bin/compact jobs | input <düğüm>   # hiyerarşik raporlar (Go; aşağıda)
./bin/photos next <k> <N> | signals | score | status   # foto değerlendirme paketleri (Haiku işçileri; src/photos/worker.md)
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

### Hierarchical recursive compaction (raporlar)

Veri bir LLM bağlamına sığmayacak kadar büyük ve her gün büyüyor. CFV onu ağaç olarak sıkıştırır:

1. **Kökten başla:** önce tüm veriyi tek seferde vermeyi dene.
2. **Sığmıyorsa böl:** düğümün ham verisi sınırı (`Limit`, ~60 bin karakter ≈ 20k token)
   aşıyorsa bir alt katmana in ve her çocuk için 1'i tekrarla (recursive):

       kök → yıl → ay → hafta (aya kırpılmış ISO hafta) → gün → 3 saat → 1 saat

   Bölme her dalda ayrı karar verilir: az veri olan 2016–2020 yılları tek parça kalır, yoğun
   bir gün 3 saatlik bloklara iner. 1 saat her zaman yapraktır (gerekirse kesilir).
3. **Aşağıdan yukarı özetle:** yaprak düğümün raporu ham veriden, bölünmüş düğümün raporu
   çocuklarının raporlarından yazılır (bölünmüş günde + günün iskeleti: takvim, ödevler,
   sağlık). Her rapor girdisinin en fazla ~%10'u kadardır; böylece her adım tek bağlama sığar.
4. **Artımlı güncelle:** her rapor kapsadığı aralığın imzasını (`sig`: yaprak/bölünmüş + kayıt
   sayısı + toplam uzunluk) taşır. Yeni veri bir aralığa girince o düğümün ve onu içeren tüm üst
   düğümlerin imzası değişir; yalnız onlar aşağıdan yukarı yeniden yazılır, kardeşlere
   dokunulmaz. Bir düğüm büyüyüp sınırı aşarsa yapraktan bölünmüşe döner, çocukları doğar.
5. **Derine inme:** üstteki ajan bir ayrıntı isterse herhangi bir düğümün raporuna, ham
   verisine (`compact input`, `compact day`) ve MCP üzerinden fotoğraflara inebilir.

Dosyalar `data/processed/reports/<yol>/index.md` (ör. `reports/2026/10/W43/22/09-12/index.md`).
Program Go ile yazıldı (`src/compact/`, ~740 satır, tek bağımlılık: saf Go SQLite):

    go build -o bin/compact ./src/compact
    ./bin/compact jobs [düğüm]       # yazılacak/bayat düğümler, çocuklar önce
    ./bin/compact input <düğüm>      # o düğümün LLM girdisi (ham veri ya da alt raporlar)
    ./bin/compact day <gün> [9-12]   # bir günün/saatin ham dökümü
    ./bin/compact send [düğüm]       # cfv-editor bp ajanına "işleri yap"
    go test ./src/compact

Raporları bp ajanı `cfv-editor` yazar (`src/compact/editor.md`); her düğümü ayrı, temiz bir alt
ajana verir. Biçim: `src/compact/report.md`. `uv run cfv ingest reports` raporları aranabilir
yapar; zaman çizelgesinde her gün kendisini kapsayan en derin rapora linklidir.
`cfv-day-report.timer` (05:00, `compact send`) şimdilik kapalı.

### Fotoğraf puanı

Her foto/video bir LLM'e (bp Haiku işçileri, `src/photos/worker.md`) tek tek gösterilir. Kart:
zaman, yer, albüm, yüz tanımadaki kişiler + `data/config/people.md` notları, o günün takvimi ve
davranış sinyalleri. LLM önce gerekçesini (düşünce izi) yazar, sonra `kalite`, `ikoniklik`,
`onem`, `insta` (1-10) verir → `data/processed/photos/eval/<asset UUID>.json`.

Sinyaller (`./bin/photos signals`, Tuna'nın davranışından; Fotoğraflar/WhatsApp verisi veri
ajanının `photos-meta.sqlite` çıkarımından):
- **favori** +1.5; art arda ≥20 favorinin içindeyse yarısı (toplu yanlış favorileme)
- **kopya**: aynı görüntü (256 bit dHash, Hamming ≤24; ekran görüntüleri hariç) galeride kaç kez
  var. İlk-son kopya arası ≥30 gün ise her ek kopya +0.5 (en çok +2); yakın tarihli kopyalar seri
  çekimdir, belge/ekran görüntüsü kopyaları sayılmaz
- **açma/paylaşma**: log2(1+açılma)/4 (en çok +1.5), paylaşma başına +0.5 (en çok +1)
- **elle emek**: açıklama yazdın +1, kendi Anı'na koydun +1, düzenledin +0.5; Apple öne çıkardı +0.5
- **WhatsApp**: gönderdiğin her sohbet +0.5 (en çok +1.5), gönderdiğinde aldığı her tepki +0.25
  (en çok +1); sana geldiğinde tepki verdin ya da 10 dk içinde güldün (😂, haha, jsjs…) +0.5 (en çok +1)

- **sticker**: bu fotodan WhatsApp stickerı yapılmışsa +1.5, gönderildikçe +0.1 (en çok +1). Eşleşme
  CLIP benzerliğiyle (≥0.95) ya da Haiku'nun sticker kartında doğrulamasıyla (`kaynak_foto`)

Vektör veritabanı (`uv run src/photos/embed.py embed | stickers | search "metin"`): galeri fotoları ve
farklı stickerlar CLIP (ViT-B/32, işlemcide) ile `data/processed/photos/vectors.sqlite`'a (sqlite-vec)
girer; Türkçe metinle arama çok dilli CLIP metin kodlayıcısıyla. Stickerlar da Haiku'ya gösterilir
(kaç kez gönderildi, kimlere, olası kaynak foto).

`puan = (onem + ikoniklik + insta) / 3 + sinyal bonusu` → `./bin/photos score` →
`data/processed/photos/scores.tsv` (yüksekten düşüğe).

### Eşleştirme

`cfv.link` takvim olaylarıyla fotoları eşleştirir (`event_link`, `photo_at`): saatli olayın
süresi içinde ±30 dk çekilen fotolar ve 2-14 günlük tüm-gün olaylarının (gezi, kamp) günleri.

### MCP

Repo kökündeki `.mcp.json` `cfv` sunucusunu tanımlar (bu klasörde açılan Claude oturumları
görür). Araçlar: `when`, `search`, `timeline`, `digest`, `week_page`, `people`, `photos`.
Sunucu veritabanını salt-okur açar.

`CFV_DATA` veri kökünü, `CFV_PHOTO_LIBRARY` foto kütüphanesini değiştirir.
