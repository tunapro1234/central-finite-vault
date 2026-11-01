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
src/cfv/          kod (schema.sql, db.py, cli.py; ileride ingest/, query.py, mcp/)
tests/            testler + sentetik fixture'lar
docs/             belgeler
data/             KİŞİSEL VERİ — git dışı, chmod 700
  raw/            ham kaynaklar (dokunma)
  processed/      cfv.sqlite ve ara çıktılar
```

## Kullanım

```sh
uv sync
uv run cfv init-db          # data/processed/cfv.sqlite oluşturur
uv run pytest
```

`CFV_DATA` ortam değişkeni veri kökünü değiştirir (varsayılan: `<repo>/data`).
