WAT dar gerçek dosya denetimi — yerel modül, canlı kapsam açmaz
=============================================================

Dosyalar:
  v11_wat_media_inspector.py
  test_v11_wat_media_inspector.py

Bu katman yalnız verilen gerçek dosya baytlarını inceler. Basecamp'ten indirme,
model çağrısı, anahtar okuma, kuruluma girişme, yorum veya mail gönderimi yoktur.
Mevcut iç deneme sağlayıcısının 2 MiB sınırı değiştirilmemiştir. Ayrı bir WAT
okuyucusu, kendi yetkili proje/görev/yorum/ek doğrulamasını yapmalıdır. Tarihsel
WAT örneklerindeki 2–4 MiB PNG'ler burada mevcut 16 MiB statik sınıra sığar.

API
---
  inspect_wat_media(data, *, declared_mime, expected_sha256, brief_sha256,
                    output_spec, source, checked_at,
                    include_text=False, frame_indices=(),
                    include_frame_bytes=False)

data tam bytes olmalıdır. expected_sha256 ile gerçek SHA-256 birebir eşleşir.
brief_sha256, çıktı bazındaki güncel briefin SHA-256 değeridir. checked_at saat
dilimi içeren ISO tarihidir. source mevcut byte inspector'ın tam şemasıdır:
  verified, account_id, project_id, task_id, attachment_id, evidence_ref
attachment_id pozitif gerçek sayısal kimlik veya güvenilir sağlayıcı adaptörünün
doğrulanmış gerçek SGID'den türettiği basecamp-sgid-sha256:<64hex> olabilir.
verified=True tek başına yetki kanıtı değildir: adaptör gerçek API kaydını,
görev/ek bağını, gönderici kimliğini ve son sürümü önceden doğrulamalıdır.

Sonuç JSON kanıtında source/brief/output_spec/dosya hashleri bulunur. artifact
mevcut kalite katmanının dosya manifest biçimindedir. checks somut teknik
ölçümleri taşır. inspection statik dosyada mevcut byte inspector sonucudur.
evidence_sha256 sonucu kendi alanı hariç bağlar; varsa aşağıda açıklanan
frame_bytes bu hashin ve JSON kaydının dışında tutulur. include_text=True
istenirse nested inspection.extracted_text de sonucu/hashini değiştirir.

delivery_authorized ve brand_or_creative_accepted DAİMA false.
technical_status=passed yalnız belirtilen teknik kontrollerin geçtiğidir.
quality score, 90/80 gönderim kararı, müşteri görünürlüğü ve onay sonucu değildir.

PNG/JPEG
--------
Mevcut v11_artifact_inspector doğrudan kullanılır: gerçek imza, MIME eşliği,
16 MiB dosya sınırı, 24 milyon piksel sınırı, PNG tam CRC/zlib/scanline kontrolü,
JPEG Pillow decode/orientation bilgisi, briefteki boyut/ölçü kontrolü.

output_spec mevcut statik şemadır. Örnek:
  {"id":"story","mime":"image/png","width":1080,"height":1920,
   "text_inspection":"tesseract",
   "required_text":[{"id":"price","text":"10,99 TL/kWh","critical":true}]}

Tesseract varsa eng+tur gerçek piksel OCR kullanılır; yoksa unassessed olur.
OCR normalleştirmesi harf büyüklüğü/aksan farklılıklarını yumuşatır; bu yüzden
Türkçe imla kontrolünün yerine geçmez. OCR'nin yazıyı görememesi kesin yokluk
kanıtı değildir. Yasak bir ifade görülmezse mevcut motor unassessed tutar.
PNG metadata yazıları görünür metin sayılmaz. Yazı, fiyat veya tarih kaynağı,
güncel çıktı bazındaki brief olmalıdır; geçmiş tasktaki her şey her görsele
uygulanmaz. Kaynak arka plan görseline kampanya-final şartı yüklenmez.

Nunito Sans / Josefin Sans ve Zircon #E8E7E6 kullanıcıca netleştirilmiş kurallardır.
Bu modül raster görüntüden kesin font dosyasını, font kullanım hakkını veya
logo master eşliğini doğruladığını iddia etmez. Kılavuzdaki fotoğraf renkleri
genel bir "palet dışı hata" değildir. Gözüken renk, bölge/renk profili/şeffaflık
bağlamı olmadan marka rengi hatası sayılmamalıdır. Model/human gözlemi ayrı,
referans kılavuz sürümünün hashine bağlı değerlendirmedir.

MP4/H.264: dar teknik aday kapsam
-------------------------------
Video desteği yalnız yerel ffprobe/ffmpeg mevcutsa kullanılabilir; modül hiçbir
şey kurmaz. Şu anki hostta bu araçlar yoktur; mevcut cloud imajı da ffmpeg
kurmuyor. Dolayısıyla bu yeni video yolu gerçek WAT video kabulü değildir.
Gerçek Samet çıktısı ve hedef runtime smoke testi ayrıca gereklidir.

Sınırlar: 64 MiB, 60 saniye, 60 fps; en çok 3840x2160 toplam piksel ve her kenar
en çok 3840; tek H.264 video, sıfır/bir AAC ses, kare pikseller; 90 derece
katları rotation. Farklı codec, MOV/QuickTime, ek stream veya belirsiz metadata
insan kontrolünde bekler. Bunlar müşteri hizmet politikası değil parserın
dar teknik kapsamıdır; genişletme yeni dosya/risk kabulü gerektirir.

output_spec:
  {"id":"motion","mime":"video/mp4","width":1080,"height":1920,
   "duration_seconds":15,"duration_tolerance_seconds":"1/20",
   "frame_rate":"30/1","audio_required":true}
Yalnız id/mime zorunlu; belirtilmeyen özellik briefte zorunlu sayılmaz.
audio_required=true ses tracki ister, false sessiz dosya ister; özellik yoksa
yalnız mevcut durum ölçülür. Ses trackinin varlığı duyulan içeriği doğrulamaz.
Süre ve fps Fraction olarak tam karşılaştırılır; varsayılan süre toleransı
0,05 saniye. Encoded ve rotation sonrası display ölçüleri birlikte saklanır.

ffprobe yalnız container/stream metadata ölçer. Başarılı metadata ölçümü bütün
video frame'lerinin decode edilebildiği anlamına gelmez: full_decode_verified
false kalır. Metadata ile dosya yapısı/oynatım/içerik kabulü aynı şey değildir.

frame_indices en çok 8 benzersiz, 0–3599 arası frame sıra numarası alır.
ffmpeg, seçilen gerçek frame'i PNG olarak decode eder; her frame gerçek byte
inspector'dan geçer. Kareler video_sha256 ve kendi sha256 değerlerine bağlanır.
frame sıra numarası zaman damgası değildir: timestamp_seconds=None,
timestamp_verified=False. Variable frame rate için ortalama fps'den sahte
zaman damgası üretilmez. metadata içinde frame_rate ortalama fps'dir.

İsteğe bağlı include_frame_bytes=True, result.frame_bytes içinde modelin
inceleyebileceği gerçek PNG baytlarını yalnız bellekte verir. Bu alan JSON'a
ve kalıcı iş kaydına yazılmamalıdır. Normal JSON kanıtında yalnız hashler,
ölçüler ve frame_index bulunur. Sonuç coverage.decoded_frame_indices der;
bu frame'ler henüz anlam bakımından değerlendirilmiş sayılmaz.

Tam video içeriği, geçişler, tüm zaman aralığındaki yazılar, son kartın doğru
bekleme süresi, seslendirme, müzik veya haklar örneklenen karelerle doğrulanmaz.
full_content_review, complete_visual_semantics, complete_audio_review daima
false kalır. Kısmi örnekleme sonucu "tam video 90+" diye otomatik teslim açamaz.

Süreç sınırları
--------------
Dosya 0700 geçici klasörde 0600 olarak tutulur, hash çocuk süreçte tekrar ölçülür.
Yalnız oluşturulan yerel input.mp4 yolu kabul edilir; URL veya symlink kabul
edilmez. shell kullanılmaz. Sabit PATH, drefs kapalı, absolute external path
kapalı, input protocol whitelist file/pipe ve demuxer mov kullanılır. Parser
diagnostikleri/credential ortamı dışarı aktarılmaz. Outer timeout 25, araç
timeout 20 saniye; CPU 20 saniye, açık dosya 64, yazılan dosya 8 MiB, Linux adres
alanı 768 MiB ile sınırlandırılır. Bulut containerının gerçek bellek/CPU sınırı
ayrıca test edilmelidir; Linux sınırlaması cloud kaynak kabulünün yerine geçmez.
İndirici için ağ güvenliği/redirect domain/byte sınırı bu modülün dışında kalır.

Bağlama yolu
------------
1. Yetkili WAT task/comment içindeki Berkay/Samet ekinin gerçek kaynak kaydını,
   güncel briefi ve hedef çıktı/aşamasını doğrula; dosyayı hash ile bağla.
2. Bu teknik kanıtı üret. Gerekli kontroller failed/held/unassessed ise sonucu
   geçer sayma. Dosya değişirse tüm ilgili kanıtlar yenilenmelidir.
3. Statik görseli veya açıkça kısmi video karelerini ayrı model/human inceleme
   adaptörüne ver; onaylı kılavuz hashini ve çıktı brief hashini zorunlu bağla.
   Görünür yazıyı, layout/okunurluğu ve marka uyumunu ayrı kanıtlarla değerlendir.
4. Generic quality gate'te kritik gereklilikler ve bilinmeyenler puanla aşılmaz.
   90+, 80–89 insan onayı, <80 revizyon kararını yalnız bütün gerekli kanıtlar
   tam olduğunda kullan; bu teknik modül tek başına bu kararı vermez.
5. Kalıcı iş kaydında kısa manifest/hash/karar tut; include_text/frame_bytes
   veya büyük kanıt paketini OperationsStore'un 60 KiB kaydına koyma. Ayrı
   kanıt dosyası referansı kullan. Yeni nesne deposu kurulumuna bu modül girmez.

PDF/PSD/ZIP
-----------
Bu wrapper PDF, PSD, ZIP ve metni görsel çıktı olarak kabul etmez; held döner.
Mevcut ayrı PDF metin çıkarıcı, görünür sayfa/layout kanıtı değildir. 39 sayfalık
güncel WAT kılavuzu runtime PDF limitine (30 sayfa) zorlanmamalıdır; onaylı
referans hazırlama işlemi olarak hash bağlı sayfa renderları kullanılmalıdır.
Kaynak PSD veya storyboard olması son video tesliminin varlığını kanıtlamaz.

Test
----
  python -m unittest test_v11_wat_media_inspector -v
Gerçek PNG/JPEG (Pillow varsa) testleri ve açıkça mock video metadata/kare
testleri ayrı tutulmuştur. Gerçek ffprobe/ffmpeg varsa ilave test küçük yerel
H.264 teknik fixture üretir, onu gerçekten probe/decode eder; yoksa skip olur.
Hiçbir test hosta araç kurmaz veya modeli/provider'ı çağırmaz. Mock testleri
gerçek video/üretici/müşteri kabulü olarak raporlanamaz.

Birincil teknik kaynaklar:
  https://ffmpeg.org/ffprobe.html
  https://ffmpeg.org/ffmpeg-formats.html#mov_002fmp4_002f3gp
  https://ffmpeg.org/ffmpeg-protocols.html#Protocol-Options
  https://ffmpeg.org/ffmpeg-filters.html#select_002c-aselect
  https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html
