# Offroad Edit Bot 🚙

Offroad grubunun turda çektiği videoları **çekim sırasına göre dizip, geçiş efektleriyle tek videoda birleştiren** bot.
Extreme anları (zıplama, çamur, rampa) kendisi bulup **slow-motion** yapar ve Premiere Pro'da
**AtomX** ile son rötuşu yapabilmen için hazır bir timeline da çıkarır.

## Ne yapıyor?

1. **Sıralama** – her videonun çekim saatini okur (sırasıyla: kamera/telefon metadata'sı → dosya adındaki tarih
   `VID_20260510_101233` → dosya tarihi). Farklı kişilerin telefonları + GoPro karışık olsa da doğru sırayı bulur.
   GoPro'nun bölünmüş dosyaları (`GX010123`, `GX020123`) de doğru sıraya girer.
2. **Hareket analizi** – her klipte hareketin en yoğun olduğu anı bulur. `--clip-len` verilirse klibin o kısmı kullanılır.
3. **Slow-motion** – en extreme anları (varsayılan: klip sayısının 1/3'ü) %40 hıza düşürür. Ses de ağırlatılır.
4. **Birleştirme** – hepsini tek MP4 yapar: karışık geçişler (zoom, whip pan, circle, flash, glitch...),
   dikey telefon videolarına bulanık arka plan, açılış başlığı, fade in/out, isteğe bağlı müzik.
5. **Premiere Pro timeline'ı** – `*_premiere.xml`: klipler sıralı, aralarında Cross Dissolve,
   her kesimde `AtomX: <önerilen geçiş>` ve extreme anlarda `SLOW-MO` marker'ı.
6. **Liste** – `*_edit_plan.csv`: hangi klip hangi sırada, nereden kesildi, slow-mo nerede.

## 🖥 Panel (önerilen)

Tarayıcıda açılan video paneli – her şey tek ekranda:

1. **Yükle** – videoları pencereye sürükle-bırak (ya da “＋ Video yükle”). Telefon, GoPro, dron karışık olabilir.
2. **Çekim sırası** – en üstte kartlar çekim saatine göre dizilir. Kartı **sürükleyerek** sırayı değiştir,
   “Dahil” işaretini kaldırarak videodan çıkar, 🗑 ile sil. “⏱ Çekim saatine göre sırala” ile otomatiğe döner.
3. **İzle** – karta tıkla, büyük oynatıcıda izle (sıralamaya dokunmadan). “▶ Hepsini sırayla izle” ile tüm turu izle.
4. **Konuşmalar** – “🎙 Yazıya dök”: konuşmalar yazıya dökülür, **küfürler otomatik kırmızı + bip** olur.
   - Kelimeye tıkla → bip ekle/kaldır
   - 🔇 → o satırı tamamen sessize al (gereksiz konuşmalar)
   - ⭐ → o konuşmayı **açılış fragmanına** ekle (altyazılı, müzik o sırada kısılır)
   - “bipli dinle” açıkken oynatıcıda bipler canlı duyulur
5. **Videoyu oluştur** – başlık, açılış fragmanı (15/30/45/60 sn), format (YouTube / Reels dikey), geçiş,
   slow-mo, müzik seç → 🎬. Bitince panelde izle, indir; Premiere Pro XML'i de indir.

**Açılış fragmanı:** en başta turun en extreme anlarından hızlı kesimler (her videodan en hareketli an)
ve ⭐ ile seçtiğin konuşmalar, çekim sırasıyla. Ardından başlık ve geçişli ana video gelir.

Açmak için: Windows'ta `panel_ac.bat`'a, Mac'te `panel_ac.command`'a çift tıkla. Ya da:

```bash
python3 panel.py proje
```

Tarayıcıda `http://localhost:8000` açılır. Videolar `proje/videolar/`, çıktılar `proje/cikti/` içinde durur.
Aynı Wi-Fi'deki arkadaşların telefondan yüklesin istersen: `python3 panel.py proje --host 0.0.0.0` →
telefondan `http://BILGISAYAR_IP:8000`.

### Küfür listesi
`kufur_listesi.txt` – istediğin kelimeyi ekle/çıkar. `kelime*` = o harflerle başlayan her şey.
Konuşma tanıma için ilk seferde Whisper modeli internetten iner (small ≈ 500 MB). Ekran kartı yoksa
“Hızlı (base)” model daha çabuk, “Doğru (medium)” daha isabetli.

## Kurulum

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/download.html) (`ffmpeg` ve `ffprobe` PATH'te olmalı)
  - Windows: `winget install ffmpeg` · macOS: `brew install ffmpeg`

- Küfür bipleme için: `pip install -r requirements.txt` (faster-whisper)

Sıralama/birleştirme için ek Python paketi gerekmez.

## Komut satırı kullanımı

Tüm videoları bir klasöre at (telefonlardan, GoPro'dan, dron'dan):

```bash
python3 offroad_edit.py ~/Videolar/kaz_daglari --title "Kaz Dağları Offroad"
```

Kısa, tempolu bir klip (her videodan en iyi 6 sn) + müzik + motor sesi kısık:

```bash
python3 offroad_edit.py ~/Videolar/kaz_daglari --title "Kaz Dağları Offroad" --clip-len 6 --music muzik.mp3 --keep-clip-audio
```

Instagram/TikTok için dikey:

```bash
python3 offroad_edit.py ~/Videolar/kaz_daglari --res 1080x1920 --clip-len 4 --music muzik.mp3
```

Başa 30 sn açılış fragmanı: `--teaser 30`. Sadece küfür bipleme: `python3 bleep.py KLASOR`.

Çıktılar `KLASOR/_edit/` içine yazılır.

### Slow-motion ayarları

| Seçenek | Açıklama |
|---|---|
| `--slowmo N` | En extreme N anı ağırlat (varsayılan klip sayısı/3, `0` = kapalı) |
| `--slowmo-speed 0.4` | Hız (0.4 = %40, yani 2.5 kat yavaş; 0.25 = 4 kat) |
| `--slowmo-len 2` | Ağırlatılacak anın süresi (sn) |
| `--slowmo-file liste.txt` | Otomatik yerine kendin seç (bkz. `slowmo_ornek.txt`) |
| `--smooth-slowmo` | 30fps çekimlerde ara kare üretir, daha akıcı ama render uzun sürer |

> İpucu: Extreme anları **60 veya 120 fps** çekerseniz slow-mo çok daha akıcı olur — bot yüksek fps'i otomatik kullanır.

### Diğer seçenekler

`--transition` (`mix` ya da tek tip: `zoomin`, `slideleft`, `fadewhite`, `dissolve`...), `--trans-dur 0.6`,
`--skip-start 1` (kayda basarken olan sarsıntıyı at), `--recursive` (alt klasörler), `--no-render` (sadece Premiere XML),
`--preset veryfast` (hızlı önizleme). Tümü için: `python3 offroad_edit.py -h`

## Premiere Pro + AtomX ile bitirme

1. Botu `--no-render` ile (veya normal) çalıştır.
2. Premiere Pro → **File › Import** → `_edit/…_premiere.xml`. Sıralı sequence açılır.
3. Timeline'da **Shift+M** ile marker'dan marker'a atla:
   - `AtomX: Zoom In / Punch Zoom` gibi marker'larda: AtomX panelinden o geçişi seçip kesimin üstüne sürükle
     (marker'daki Cross Dissolve'u silebilir ya da üstüne yazabilirsin).
   - `SLOW-MO` marker'ında: klibe sağ tık → **Time Remapping › Speed**, marker aralığına keyframe koyup %40'a indir.
4. Müzik ekle, renk düzelt, export.

> Not: AtomX geçişleri AtomX'in kendi panelinden uygulanır; dışarıdan bir dosya ile otomatik eklenemiyor.
> Bot bu yüzden her kesim için uygun AtomX geçişini marker'a yazıyor, sen sadece sürükleyip bırakıyorsun.
