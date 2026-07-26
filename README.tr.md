# 📋 CopyTracker

> English documentation: [README.md](README.md)

Pano gelen kutusu / link triage aracı: Kopyaladığın **her şeyi** (Ctrl+C) anında
yakalar, sağ altta bildirim gösterir ve web arayüzünde to-do listesi gibi
işlemeni sağlar. Linke tıkla → açılır → ✓ işaretlenir → aşağı düşer.

![CopyTracker ekran görüntüsü](docs/screenshot.png)

## Kurulum (bir kere)

```
pip install -r requirements.txt
```

## Çalıştırma

| Ne | Nasıl |
|---|---|
| Arka planda başlat + listeyi aç | `start.bat` (çift tıkla) |
| Konsolda çalıştır (logları gör) | `python copytracker.py` |
| Durdur | Tepsi simgesi → **Çıkış** ya da `stop.bat` |

Web arayüzü: **http://localhost:8765** · Tepsi simgesi: sağ tık → menü

**Kısayollar:** `Ctrl+Alt+K` yakalamayı aç/kapat · `Ctrl+Alt+L` listeyi aç

## Veriler nerede durur?

| Veri | Yer | Davranış |
|---|---|---|
| **Aktif liste** | RAM | Geçici — uygulama kapanınca gider (bilinçli tasarım) |
| **Arşiv** | `archive.json` | Sadece sen "Arşivle" deyince yazılır; süresi dolunca otomatik silinir |
| **Ayarlar + koleksiyonlar** | `settings.json` | Kalıcı |
| Çökme güvenlik ağı | `session_backup.json` | Uygulama çökerse bir sonraki açılışta "kurtarılsın mı?" diye sorar; temiz kapanışta silinir |

**Arşiv saklama süresi** (Ayarlar'dan): 1 saat · 1 gün · Gün sonu · **1 ay
(varsayılan)** · Sonsuz. Süresi dolan arşiv kayıtları otomatik silinir.

## Özellikler

- **Yakalama Aç/Kapat** — üstteki şalter, tepsi menüsü ya da Ctrl+Alt+K
- **Yakalama filtresi** — Tümü / 🔗 Sadece linkler / 📷 Sadece Instagram /
  🎯 Özel alan adları (kendi listeni yaz: `youtube.com, x.com…`)
- **Görünüm filtresi** — kaydedilmişleri süz: Tümü / Linkler / Instagram / Metinler
- **Koleksiyonlar** — kütüphane aç, kopyalananlar aktif koleksiyona akar;
  silinen koleksiyonun öğeleri Genel'e taşınır
- **Tarihsel geçmiş** — Bugün / Dün / tarih başlıklarıyla gruplu + arama
- **Link başlığı** — linkin sayfa başlığı otomatik çekilir, çıplak URL yerine görünür
- **Akıllı tekrar** — aynı içerik tekrar kopyalanınca yeni kayıt açılmaz, ×2
  rozeti alır; tamamlanmışsa geri açılır
- **Listeyi kopyala / ⬇ .txt indir** — görünen listeyi tek tıkla dışarı al
- **Seçim modu** — çoklu seç → topluca kopyala / arşivle / sil
- **📌 Sabitleme** — sabitlenen öğe üstte durur, Temizle'den etkilenmez
- **🗄 Arşiv** — tek öğe, seçim ya da "tamamlananları arşivle"; arşivden geri
  yükleme ve kalıcı silme
- **▦ QR kod** — linki telefonla okutup mobilde aç
- **⧉ Geri kopyalama** — öğeyi panoya kopyalar (yeniden kaydedilmez)

## Gizlilik ve güvenlik

- Her şey yerelde kalır (`127.0.0.1`); tek dış istek link başlığı ve site ikonu çekimidir.
- Parola yöneticilerinin "izleme dışı" işaretlediği kopyalar asla kaydedilmez.
- Yerel API, CSRF'e karşı korunur (özel başlık + origin denetimi) — ziyaret
  ettiğin web siteleri listeni okuyamaz ve silemez.
- `settings.json`, `archive.json`, log ve oturum dosyaları kişisel pano verisi
  içerir — `.gitignore`'dadır, asla commit'leme.

Güvenlik açığı bulursan lütfen herkese açık issue açmak yerine bakımcıya
e-posta ile ulaş.

## Notlar

- Bildirimler uygulamanın kendi penceresiyle gösterilir — Windows bildirim
  ayarlarından bağımsız çalışır; tıklayınca liste açılır.
- Resim/dosya kopyaları kaydedilmez; "metin değil" bildirimi gösterilir.

## Lisans

[MIT](LICENSE)
