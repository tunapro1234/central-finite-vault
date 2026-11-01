# Yol haritası (öneri — Tuna onayı bekliyor)

## Faz 0 — Temel ✅ (2026-11-01)
Repo iskeleti, `.gitignore`, uv/pyproject, README, veri manifestosu, şema taslağı v0,
`cfv init-db`, sentetik şema testi.

## Faz 1 — Fotolar + kişiler (veri hazır)
- `ingest/photos.py`: Photos.sqlite'ı `immutable=1` ile aç → `photo`/`video` event'leri,
  `person` + `person_alias` (ZPERSON), `event_person` (ZDETECTEDFACE).
- `ingest/icloud_files.py`: `~/Pictures/icloud-sync` taraması → `media`; asset ↔ dosya
  eşleme (orijinal ad + zaman). Eşleşmeyenler için rapor (`data/processed/reports/`).
- Çıktı: "X kişisinin olduğu, şu aralıktaki fotolar" sorgusu çalışır.

## Faz 2 — iPhone yedeği (yedek bitince)
- `ingest/backup.py`: Manifest.db üzerinden domain/relativePath → hash'li dosya çözümü.
- Notlar (NoteStore protobuf → düz metin), Takvim, MediaLibrary (parça meta), Contacts.

## Faz 3 — İlk MCP (salt-okur)
Araçlar: `timeline(start, end, kinds)`, `period_digest(start, end)` (fotolar+kişiler,
top müzik, takvim, notlar), `search(text)`, `photos_with(person, ...)`,
`top_tracks(start, end)`. Fotoğraf dosyasını Claude'a yalnızca açık istekle verir
(küçültülmüş). → **Instagram postu senaryosu burada çalışır.**

## Faz 4 — Dış exportlar (geldikçe)
Apple Music Play Activity → Snapchat → BeReal → YouTube. Her biri tek bir
`ingest/<kaynak>.py` + sentetik fixture testi.

## Faz 5 — Zenginleştirme (sonra)
Kişi birleştirme önerileri, yer kümeleme/ters geocoding (yerel), Immich entegrasyonu
(Tuna kararına bağlı), dönem otomatik "anı" önerileri.
