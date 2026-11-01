# Veri manifestosu

Her kaynak için: nerede durur, formatı, ne çıkarılacak, durumu. Yollar `data/` köküne
göredir (aksi belirtilmedikçe). Bu belge **kişisel içerik barındırmaz** — yalnızca
yapı ve sayımlar.

Durum anahtarı: ⬜ bekleniyor · 🟨 kısmi/sürüyor · 🟩 ham veri hazır · ✅ import edildi

| # | Kaynak | Yer | Format | Çıkacak olan | Durum |
|---|--------|-----|--------|--------------|-------|
| 1 | iPhone Photos kütüphanesi DB | `raw/iphone/photos-db-2026-11-01/Photos.sqlite` | SQLite (Core Data), 1.1 GB | asset zamanı/konumu, orijinal dosya adı, yüz → kişi | 🟩 (28.602 asset, 218 isimli kişi, 34.599 yüz) |
| 2 | Foto/video dosyaları | `~/Pictures/icloud-sync/YYYY/AA/` (data/ dışında) | HEIC/JPG/MOV + XMP sidecar | `media` satırları (yol, hash, boyut) | 🟨 icloudpd ~%87 (photos ajanı yönetir) |
| 3 | iPhone tam yedeği (şifresiz) | `raw/iphone/backup/<UDID>/` | idevicebackup2 (Manifest.db + hash'li dosyalar), 22 GB, 78.195 dosya | aşağıdaki 3a–3d (+ mesajlar, arama geçmişi, uygulama verileri) | 🟩 |
| 3a | ↳ iCloud Notlar | yedekte `NoteStore.sqlite` (AppDomainGroup-group.com.apple.notes) | SQLite; gövde gzip'li protobuf | not başlığı, metni, oluşturma/değişiklik zamanı | 🟩 yedekte |
| 3b | ↳ Takvim | yedekte `Library/Calendar/Calendar.sqlitedb` (HomeDomain) | SQLite | etkinlik, süre, konum, katılımcı | 🟩 yedekte |
| 3c | ↳ Müzik kütüphanesi | `raw/iphone/afc-2026-11-01/iTunes_Control/iTunes/MediaLibrary.sqlitedb` | SQLite | 4.019 parça (2.614 çalınmış): `item_stats` çalma/skip sayısı, son çalma, beğeni (geçmiş YOK) | 🟩 |
| 3d | ↳ Kişiler (Contacts) | yedekte `AddressBook.sqlitedb` (HomeDomain) | SQLite | kişi adları → `person_alias` eşleşmesine yardım | 🟩 yedekte |
| 3e | Cihaz içi foto indeksleri (AFC dökümü) | `raw/iphone/afc-2026-11-01/PhotoData/Caches/…`, `MediaAnalysis/mediaanalysis.db` | SQLite | `psi.sqlite`: sahne etiketi, OCR, yer adı; `CLSLocationCache`: konum → yer adı; estetik skorları | 🟩 |
| 4 | Apple Music Play Activity | privacy.apple.com export → `raw/apple-privacy/` | CSV (`Apple Music - Play History Daily Tracks` / `Play Activity`) | her dinleme: zaman, parça, süre | ⬜ Tuna talep edecek |
| 5 | Snapchat My Data | `raw/snapchat/` | ZIP → JSON/HTML (memories_history, chat_history, ...) | memory'ler, snap/sohbet zamanları, karşı taraf | ⬜ Tuna talep edecek |
| 6 | BeReal GDPR | `raw/bereal/` | ZIP → JSON + görseller | her BeReal: zaman, ön/arka foto, konum | ⬜ Tuna talep edecek |
| 7 | Google Takeout — YouTube | `raw/google-takeout/` | `watch-history.json` (JSON seç, HTML değil) | izleme zamanı, video başlığı, kanal | ⬜ Tuna talep edecek |

## Notlar / bilinen kısıtlar

- **Şifresiz yedek** Sağlık, Anahtar Zinciri, Safari geçmişi ve bazı konum verilerini
  içermez. Bunlar istenirse şifreli yedek gerekir (şimdilik kapsam dışı).
- **Müzik geçmişi** yalnızca Apple'ın privacy exportunda var; telefon DB'si sadece toplam
  çalma sayısı ve son çalma zamanı verir. Dönem bazlı "o yaz ne dinledim" için #4 şart.
- **Google Takeout**'ta YouTube geçmişi için format olarak **JSON** seçilmeli
  (varsayılan HTML).
- **Foto eşleme:** Photos.sqlite asset'i ↔ diskteki dosya: `ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`
  + çekim zamanı (aynı dosya adı birden çok kez geçebilir: IMG_0001 tekrarları).
  icloudpd XMP sidecar'ı ek ipucu.
- Ham dosyaların sahibi photos ajanıdır; bu repo yalnızca okur.

## Güncelleme kaydı

- 2026-11-01 — manifest oluşturuldu (Photos.sqlite hazır, yedek sürüyor).
- 2026-11-01 — yedek tamamlandı (22 GB), AFC dökümleri eklendi. Ayrıntılı ham envanter: `data/raw/README.md` (photos ajanı). Takvim kaynağı (yedek mi, iCloud/Google + vdirsyncer mi) henüz kararlaştırılmadı.
