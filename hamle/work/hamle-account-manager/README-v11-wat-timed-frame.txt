WAT gerçek video zaman damgası kanıtı

API: inspect_deadline_frame(mp4_bytes, expected_sha256=sha256,
                           deadline_seconds='2', include_frame_bytes=False)

Amaç: Aynı gerçek MP4 içinde son teslim zamanını geçmeyen bir görüntü
karesini gerçek sunum zaman damgasıyla bağlamak. ffprobe'un çözdüğü PTS ve
zaman tabanı kullanılır; kare numarası / ortalama FPS hesabı kullanılmaz.
Seçilen kare aynı SHA256 dosyadan ffmpeg ile çözülür. ffmpeg showinfo PTS'si
ffprobe PTS'siyle kesir olarak bire bir eşleşmezse inceleme bekler.

Başarılı teknik çıktı: status=verified_timestamp_frame. frame alanında
gerçek PTS, zaman tabanı, başlangıca göre geçen süre, PNG SHA256 ve boyutu
bulunur. include_frame_bytes=True yalnız bellekte frame_data PNG baytlarını
ekler. Kalıcı JSON yazmadan önce frame_data alanı çıkarılmalıdır.

Bu modül metin okuması yapmaz. Kampanya mesajının 2 saniyeden önce görünme
koşulu ancak bu doğrulanmış karede gerekli metnin ayrıca görülmesiyle olumlu
kanıt alabilir. Karede metin bulunamaması koşulun başarısızlığı değildir;
sonuç değerlendirilmemiş kalır. İlk görünme anı, videonun tümü, ses metni,
yaratıcı kalite ve müşteriye teslim yetkisi bu kanıtla doğrulanmaz.

Kapsam: en çok 64 MiB, 60 saniye, 3600 kare, H.264 MP4; gerçek süre en az
istenen son süre kadar olmalıdır. Yerel geçici dosya dışında giriş yoktur;
ağ protokolleri ve dış MOV veri başvuruları kapalıdır. Her ayrıştırıcı ayrı
CPU/bellek/çıktı/süre sınırı olan alt süreçte çalışır. Eksik araç, zaman,
hash veya çözücü uyumsuzluğunda held döner ve otomatik yeniden deneme yapmaz.

Kanıt düzeyleri:
- test_v11_wat_timed_frame.py: değişken PTS, sınır, zaman başlangıcı,
  ffprobe/ffmpeg uyumsuzluğu, SHA ve bekleme davranışı birim testleri.
- wat_media_smoke.py: kök görev sahibinin Linux imajında üretilen gerçek
  küçük MP4 ile araç ve PTS sözleşmesi kapısı.
- Gerçek Samet videoları: bulut gölge çalışmasının ayrıca kaydettiği sonuç.
  Birim testlerinin geçmesi gerçek video kabulünün yerine geçmez.
