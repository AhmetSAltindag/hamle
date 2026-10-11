Hamle v1.1 — çok aşamalı üretimin bulut bağlantısı
10 Ekim 2026

Kapsam
Bu sürüm yalnız HAMLE-IC-URETIM-20261010-01 konulu yeni Gamechangers iç
talebini kapsar. Gerçek Gmail girdisi, 5898865 / 49184053 özel Basecamp
projesi ve Hamle 53358100 kimliği kullanılır. WAT canlı gönderimi açılmaz.
Üretilen PNG'ler kontrollü teknik test dosyalarıdır; Berkay/Samet üretimi
veya WAT marka/sanatsal kalite kabulü olarak sunulmaz.

Bulut yolu
v11_internal_worker.py mevcut hamle-account-manager Cloud Run işinde çalışır.
v11_internal_state.py, hamle-operations Firestore veritabanında eski işlerden
ayrı kalıcı durum ve işlem kayıtları tutar. Kaynak konusu/ileti kimliği,
hash'i, süre, kontrol nesli, bütçe ve özel proje her yazımdan önce sınırdır.
Yeni hizmet hesabı, IAM yetkisi veya OpenAI anahtarı oluşturulmaz.

--phase advance sıradaki tek adımı kalıcı kayıttan seçer:
1. Ana görsel görevi -> tarihi kasıtlı eksik dosya -> gerçek OCR -> revizyon.
2. Düzeltilen ana dosya -> gerçek OCR -> mevcut kalite motoruyla teknik kabul.
3. Bu kabulün ardından bağlı story görevi -> gerçek dosya kontrolü -> kabul.
4. İki güncel PNG tekrar indirilir ve kontrol edilir; doğrulanmış kalite notları
   ve güncel mail yetkisiyle aynı yeni zincire tek teslim yapılır. Sent geri
   okuması alıcıları, gövdeyi, zinciri ve iki ekin tam byte hash'ini doğrular.

Üretim aşaması, başlatılmış işlem veya kalite kaydı belirsizse işlem durur.
Yalnız hazırlanan ancak gönderilmemiş işlem devam edebilir. Başlamış/belirsiz
POST otomatik tekrarlanmaz. Sağlayıcı yanıtı, geri okuma kontrolünden önce
saklanır. Gönderim doğrulanıp son durum kaydı kesilirse aynı mail yeniden
gönderilmeden kalıcı doğrulanmış kanıtla son durum tamamlanabilir.

Zamanlama
Mevcut worker.py scheduled_review yolu, zaman dilimi kaydı ve genel durdurma
kontrolünden sonra yalnız BAŞLATILMIŞ bu iç pilotu ilerletir. Zamanlayıcı
kendiliğinden yeni pilot başlatmaz. Tamamlanmış pilot için yeni işlem yoktur.
İç pilot başlatıldığında en fazla üç saat ve mevcut 12 Ekim bütçe sonuna kadar
geçerlidir. Bu sınır, müşteri işlerinin gecikmiş yetki sorununu çözmüş sayılmaz.
Gerçek zamanlayıcı tetiklenmesi ile elle başlatılan Cloud Run çalışmaları
kanıt kayıtlarında ayrı tutulmalıdır.

Dosya kontrolü
Dosyalar Basecamp'ten gerçekten indirilir. Boyut/biçim/hash kontrol edilir;
PNG pikselleri Linux Tesseract eng+tur ile okunur. OCR yapılamadıysa bekletilir.
65/100 veya 100/100 iç test puanları yalnız somut teknik koşulları temsil eder.
Video, PSD/ZIP, marka uygunluğu, sanatsal kalite ve müşteri teslim kabulü bu
dar kontrolle doğrulanamaz. Üretim motorunun bağımlılık/soru/revizyon modeli
ayrıdır; gerçek WAT üretici olaylarının bulut adaptasyonu ayrıca kabul ister.

Doğrulama ve yayın
package_cloud_source.py yalnız açık dosya listesini paketler, özel bağlantı
dosyalarını almaz. Docker derlemesi internal_ocr_smoke.py ile üç gerçek Linux
OCR kontrolü geçirir; başarısız imaj yayınlanmaz. V8 ve V9 derleme/çalıştırma
paketleri mevcut ortak bütçede toplam 2 USD pay ayırdı; bu gerçekleşmiş fatura
değildir. Dağıtım ve çalıştırma
girişimleri bir kez kaydedilir; belirsiz sonucu olan girişim yeniden verilmez.
Somut bulut sonuçları: hamle/outputs/cloud-v11-internal-20261010/ dizini.

V9 düzeltmesi
İlk gerçek bulut turunda dosya doğru okunmasına rağmen kalite kontrol zamanı
genel durdurma kaydının zamanından birkaç mikrosaniye önceydi. Sistem güvenli
biçimde bekledi. V9 tek değerlendirme zamanı kullanır ve mevcut gerçek dosyayı
yeniden okuyarak kaliteyi değerlendirir; eski görev veya yüklemeyi tekrarlamaz.
İlk hatanın kaydı korunur. İlerleyen saat ve kaldığı yerden devam senaryoları
yerel testlerle ayrıca doğrulandı.
