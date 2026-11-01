# Veri manifestosu

Her kaynak için: nerede durur, formatı, ne çıkarılacak, durumu. Yollar `data/` köküne
göredir (aksi belirtilmedikçe). Bu belge **kişisel içerik barındırmaz** — yalnızca
yapı ve sayımlar.

Durum anahtarı: ⬜ bekleniyor · 🟨 kısmi/sürüyor · 🟩 ham veri hazır · ✅ import edildi

| # | Kaynak | Yer | Format | Çıkacak olan | Durum |
|---|--------|-----|--------|--------------|-------|
| 1 | iPhone Photos kütüphanesi DB | `raw/iphone/photos-db-2026-11-01/Photos.sqlite` | SQLite (Core Data), 1.1 GB | asset zamanı/konumu, orijinal dosya adı, yüz → kişi | 🟩 (28.602 asset, 218 isimli kişi, 34.599 yüz) |
| 2 | Foto/video dosyaları | `~/Pictures/icloud-sync/YYYY/AA/` (data/ dışında) | HEIC/JPG/MOV + XMP sidecar | `media` satırları (yol, hash, boyut) | 🟨 icloudpd ~%87 (photos ajanı yönetir) |
| 3 | iPhone tam yedeği (şifresiz) | `raw/iphone/backup/` | idevicebackup2 (Manifest.db + hash'li dosyalar) | aşağıdaki 3a–3d | 🟨 yedek alınıyor |
| 3a | ↳ iCloud Notlar | yedekte `NoteStore.sqlite` (AppDomainGroup-group.com.apple.notes) | SQLite; gövde gzip'li protobuf | not başlığı, metni, oluşturma/değişiklik zamanı | ⬜ yedek bitince |
| 3b | ↳ Takvim | yedekte `Library/Calendar/Calendar.sqlitedb` (HomeDomain) | SQLite | etkinlik, süre, konum, katılımcı | ⬜ yedek bitince |
| 3c | ↳ Müzik kütüphanesi | yedekte `MediaLibrary.sqlitedb` (MediaDomain) | SQLite | parça meta + çalma sayısı/son çalma (geçmiş YOK) | ⬜ yedek bitince |
| 3d | ↳ Kişiler (Contacts) | yedekte `AddressBook.sqlitedb` (HomeDomain) | SQLite | kişi adları → `person_alias` eşleşmesine yardım | ⬜ yedek bitince |
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
