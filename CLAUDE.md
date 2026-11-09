# Offroad Edit Bot – proje notları

Offroad grubunun tur videolarını otomatik kurgulayan araç. Kullanıcı Türkçe konuşur; cevaplar,
arayüz metinleri ve komut çıktıları Türkçe olmalı. Kullanıcı yazılımcı değil: adımları sade anlat.

## Amaç / kullanıcının istekleri
- Videolar **çekim sırasına** göre dizilir (metadata → dosya adındaki tarih → dosya tarihi; GoPro parçaları).
- Videolar **geçiş efektleriyle tek videoda birleştirilir**.
- Son rötuş **Adobe Premiere Pro + AtomX** (Mister Horse) geçişleriyle yapılır. AtomX dışarıdan
  otomatik uygulanamaz → her kesime `AtomX: <öneri>` marker'ı konur, kullanıcı Shift+M ile gezip sürükler.
- **Extreme anlara slow-motion** (hareket analizi ile otomatik).
- **Küfürlü / gereksiz konuşmalar biplenir** (Whisper ile yazıya dök + `kufur_listesi.txt`).
- Video yapısı (kullanıcının örnek videosundaki gibi):
  **30 sn açılış fragmanı (en kritik anlar + bazı konuşmalar altyazılı) → LOGO → ana video**.
- **Panel**: videoları yükle, üstte sıralamayı gör/sürükleyerek değiştir, seçmeden izle.

## Dosyalar
- `offroad_edit.py` – çekirdek motor + CLI. `probe` (meta/çekim zamanı), `analyze_motion` (ffmpeg scene
  skoru), `plan`, `plan_slowmo`, `build_teaser`, `render` (ffmpeg xfade filtre grafiği, dikey videoya
  bulanık arka plan, altyazı, müzik ducking, progress callback), `render_logo`, `concat_videos`,
  `premiere_xml` (FCP7 xmeml: fragman+logo+ana video, A2 müzik, Cross Dissolve + marker'lar),
  `produce` (hepsini sırayla yapan ortak fonksiyon – CLI ve panel bunu kullanır).
- `bleep.py` – faster-whisper ile Türkçe, kelime zamanlı transkript; `auto_bleeps`; `apply_bleeps`
  (görüntü kopyalanır, ses: 1 kHz bip ya da sessiz; çekim tarihi metadata'sı korunur).
- `kufur_listesi.txt` – `kelime` = tam eşleşme, `kelime*` = ile başlayan. Kısa köklere `*` koyma (göt*, am*).
- `panel.py` – stdlib HTTP sunucu (ek bağımlılık yok). Proje klasörü: `videolar/ muzik/ logo/ cikti/
  .cache/{thumbs,transcripts,bip}` + `panel.json` (sıra, hariç tutulanlar, bipler, ⭐ alıntılar, ayarlar).
  Yükleme = PUT /api/upload (ham gövde), Range destekli video sunumu, arka plan işleri (jobs).
- `static/panel.html` – tek dosya arayüz (vanilla JS): sıralama şeridi (drag&drop), oynatıcı (canlı bip
  önizleme), transkript (kelimeye tıkla = bip, 🔇 satırı sessize al, ⭐ fragmana ekle), render ayarları.
- `panel_ac.bat` / `panel_ac.command` – çift tıkla aç. Varsayılan adres http://localhost:8000.

## Test
ffmpeg ile sentetik klipler üretip dene (testsrc2/smptebars, `-metadata creation_time=...`, biri dikey,
biri sessiz, biri ortasında yoğun hareketli). Panel için: `python3 panel.py /tmp/x --no-browser`,
curl ile PUT yükle, `/api/render` çağır, Playwright ile ekran görüntüsü al.

## Bilinen sınırlar / doğrulanmamışlar
- Premiere XML gerçek Premiere'de henüz denenmedi (sadece XML geçerliliği kontrol edildi).
- Whisper ile gerçek Türkçe konuşma testi yapılmadı (bulut ortamında huggingface.co erişimi yoktu).
- Kullanıcının örnek videosu henüz görülmedi (sohbet eki 30 MB sınırı; Google Drive linki önerildi).

## Olası sonraki işler (kullanıcıya önerildi)
- "Tek tuşla otomatik edit" butonu: tüm videoları yazıya dök + bipler + render.
- Örnek videoya göre fragman temposu / kesim süreleri / logo girişini ayarlamak.
- Premiere içinde açılan panel (CEP/UXP eklentisi).
- Müzik vuruşlarına göre kesim (beat-sync).
