GAMECHANGERS — ÇEVRİMDIŞI İŞLEM ÇEKİRDEĞİ / SINIRLI KAVRAM KANITI

Durum: Yerel olarak yapılandırıldı ve sentetik kurallarla test edildi.
Cloud'a kurulmadı. Gerçek e-posta veya Basecamp işlemi göndermez.
Bu klasör dört bağımsız ajan veya canlı işletim sistemi değildir.
Model/API çağrısı yapmaz; GPT-6 Astra / Ultra bağlantısını doğrulamaz.

ÇALIŞTIRMA
Python 3.9+ ve Europe/Istanbul saat dilimi verisi gerekir. Harici paket yoktur.
Bu klasörde terminal açıp çalıştırın:
  python3 run_tests.py
Komut 35 testi yürütür; test-results.json, demo-evidence.json ve
demo-synthetic.sqlite dosyalarını yeniden üretir. Yalnız bu isimli sentetik
demo veritabanını yeniler. Gerçek müşteri dosyası eklemeyin.

DOSYALAR
engine.py              : Çalışan çevrimdışı kontrol ve kayıt kodu.
run_tests.py           : 14 ilk senaryo + 8 altyapı + 13 yeni To/CC yetki testi.
test-results.json      : Her testin adı, kapsamı ve başarılı/hatalı sonucu.
demo-synthetic.sqlite  : İncelenebilir kalıcı sentetik kayıt örneği.
demo-evidence.json     : Aynı örneğin okunabilir veri dökümü.
test-run.log          : Son doğrulama çalışmasının konsol kaydı.

GERÇEKTEN ÇALIŞAN PARÇALAR
- SQLite içinde kaynak olayları, işler, brief sürümleri, eylemler, eylem
  günlüğü, kontrol noktaları, okuma hataları ve dosya sürümüne bağlı onaylar.
- E-posta ve Basecamp olaylarını ortak alanlara dönüştüren örnek kayıt API'si.
- Açıkça aynı talep kimliği verilmiş iki kanalın tek işe bağlanması.
- Aynı kaynak olayını ve aynı eylem anahtarını tekrar kaydetmeme.
- Aynı eylem anahtarı altında değiştirilmiş içeriği reddetme.
- Brief geçmişinin korunması; SQL güncelleme/silme engeli; yeni sürümde
  önceki sürümü kullanan bekleyen/başlamış eylemlerin durdurulması.
- Markaya özgü alıcı, Basecamp proje ve onaycı listeleri; dış iletişimde
  müşteri temsilcisi rolü; üretim rollerine sınırlı brief alanları sunma.
  Rol kimlikleri ana paketin kimlikleriyle aynıdır: account_manager,
  copywriter, graphic_designer, motion_designer.
- Her markanın izinlerini bağımsız uygulayan çok markalı sentetik yapı.
  SYNTHETIC_A / SYNTHETIC_B gerçek pilot marka seçimi değildir.
- Yanlış marka, alıcı, zincir, güncel olmayan bilgi, okunamayan varlık,
  eksik brief, kaynaklandırılmamış iddia ve yetkisiz termin/kapsam engelleri.
- İnsan devralma; durdurma bayrağını bekleyen işlemde ve sentetik gönderim
  sınırında tekrar kontrol etme; iki bağlantıda tek işlem başlatma.
- Gönderim niyeti sonrası kesintide “unknown” durumu ve otomatik tekrar
  engeli. Sağlayıcı kanıtı verilmeden sonucu kabul etmeme. “Kabul edildi”,
  “geri döndü” ve “gönderilmediği doğrulandı, inceleme gerekiyor” ayrımları.
- Başarısız okumada son başarılı kontrol noktasını koruma.
- Arşiv ve otomatik yanıt olaylarından canlı iş başlatmama.
- Son gözlenen gerçek gelen mesajın doğrudan To veya CC alanında tam
  hamle@gamechangers.com.tr adresi bulunmasını zorunlu tutma. Büyük/küçük
  harf farkı normalleştirilir; alias, artı eki veya benzer alan adı kabul edilmez.
- Brief ve dış eylemi yetkili kaynak mesajına bağlama. To/CC'den çıkarılma
  meşru biçimde gözlendiğinde ilgili zincirde bekleyen/henüz sentetik etki
  üretmemiş mail ve Basecamp eylemlerini durdurma. Yeni açık dahil edilme
  yeni brief'e izin verir; eski durdurulmuş eylemi yeniden oynatmaz.
- Europe/Istanbul saatinde Pazartesi–Cuma, 08:30 ve 18:30 dahil, her
  30 dakikada bir 21 kontrol zamanını hesaplama. Cuma 18:30 sonrası ilk
  kontrol pazartesi 08:30. Tatil takvimi bağlı değildir; hafta içi kuralıdır.

TEST KAPSAMI VE SINIRLAR
35 testin ilk 14'ü ilk talimattaki 14 durumun yapısal kural karşılıklarıdır.
Eksik alanlar, marka kimliği, okunabilirlik, eski bilgi, çelişki, kapasite,
iddia kaynağı ve yetki listeleri test verisinde açıkça verilir. Sistem bu
özellikleri doğal dilden çıkarmayı bu testlerde öğrenmez veya kanıtlamaz.
İlk brief testi yalnız örnekte zorunlu seçilen üç alanı kontrol eder; gerçek
işin bütün üretim ihtiyaçlarını kapsayan evrensel brief doğrulayıcı değildir.

Bir mesajın başka kanaldaki taleple aynı olduğu request_ref ile önceden
verilir. Eylem anahtarının doğru atanması çağıran katmanın sorumluluğudur.
Anlamsal eşleştirme ve farklı anahtarlarla aynı niyetin tekrar oluşturulması
ayrıca test edilmelidir. Rol ve marka parametreleri güvenilir çağıran katman
tarafından sağlanmalıdır; örnek kimlik doğrulama hizmeti içermez.

YENİ TO/CC KURALI — 8 EKİM 2026
23–35 numaralı testler gerçek çalışan SQLite yetki kontrolünü sınar.
Önceki mesajda bulunma, gövdedeki adres, alıntı/yönlendirilmiş To/CC, BCC,
Ahmet'in kutusunda görünürlük veya sunucu teslim kopyası izin sağlamaz.
Kendi gönderimi yanıt döngüsü başlatmaz. Eksik/şüpheli başlık kanıtı ve
belirsiz mesaj sırası izin vermez. Eski sıradaki mesajın sonradan okunması
güncel yetkiyi değiştirmez. Arşiv/öğrenme tekrarı, önceden canlı görülmüş
aynı kimliği kullanırken de iş döndürmez. Diğer kanaldaki aynı iş bildirimi
mail zincirindeki çıkarılmayı aşamaz; o zincirde yeni iş de oluşturamaz.
Yeniden dahil edilme öncesindeki Basecamp olayı tekrar yetki kazanmaz;
brief yeni yetki dönemindeki olaya bağlanır. Bağımsız yetkili zincir çalışabilir.

mail_evidence alanı güvenilir bağlayıcıdan gelmesi gereken yapılandırılmış
test verisidir: gerçek mesaj/zincir kimliği, doğrulanmış doğrudan başlıklar,
yalın From/To/CC adresleri, gelen/giden yönü ve zincirdeki güvenilir sıra.
sequence, yerel okuma sırası, email Date veya Gmail historyId'si olduğu
iddia edilen bir alan değildir; gerçek bağlayıcının bu sırayı doğru kurması
ayrıca doğrulanmalıdır. verified_direct_headers=True değerini bağlayıcı
yerine müşteri gövdesinden/LLM tahmininden almak güvenli değildir.
Sırası bilinmeyen gözlem kalıcı olarak bekletilir; daha büyük sequence tek
başına yeniden izin vermez. Bağlayıcının verified_newer_than_source_ids
alanıyla yeni olayın bekleyen her belirsiz gözlemden sonra geldiğini
doğrulaması gerekir. Bu alan gövdeden veya model tahmininden üretilemez.
Bu örnek MIME/Gmail başlıklarını çekmez veya sağlayıcıyı doğrulamaz.

Kural, yalnız meşru olarak gözlenmiş olaylara uygulanır. Görünmeyen bir
maili veya katılım kaldırılmasını izleyebildiğimiz iddia edilmez. Mevcut
bir native Basecamp olayı, önceki dar örneğin izinleri içinde kalır; bu
değişiklik gerçek Basecamp yetki kontrolü bağlandığı anlamına gelmez.
Gerçek gönderim zaten kabul edilmişse geri alınmaz; sonucu belirsiz olan
eylem belirsiz kalır ve yeniden gönderilmez. Eski şemadaki brief'lerde
mesaj yetki bağı yoksa yeni yetkili olay/brief olmadan gönderim engellenir.

14. testte dış metin çalıştırılmaz, metin olarak saklanır; izinler bu metne
göre değişmez ve listede olmayan alıcı reddedilir. Bu test herhangi bir
LLM'nin prompt injection saldırılarına dayanıklı olduğunu KANITLAMAZ.
Anahtar kelimeyle saldırı yakalama veya gerçek model güvenlik testi yoktur.

Gerçek Gmail/Basecamp geçmiş okuma, ek inceleme, dosya teslimi, cloud
zamanlama, sağlayıcı uzlaştırması, otomatik takip, alarm/kaçırılan tur izleme,
maliyet ölçümü ve üretim araçları bağlı değildir. Gerçek müşteriye gönderim
sayısı sıfırdır. Kanıt olarak “simulated_accepted” yazılır; “gönderildi” denmez.
Belirsiz demo olayı özellikle “unknown” bırakılır ve tekrar gönderilemez.
Demo ayrıca çıkarılınca durdurulan sentetik Basecamp eylemini ve sonraki
doğrudan CC ile yeni brief oluşturulmasını gösterir; her çalıştırmada bu
isimli demo veritabanı temizden kurulur.

SQLite işlemi ile dış servis gönderimini tek atomik işlem yapmak mümkün
değildir. Buradaki son kontrol yalnız sentetik sınırda doğrulanmıştır.
Canlı bağlayıcı; kalıcı outbox, sağlayıcı kimlikleri, son durum uzlaştırması,
kilit süresi/fencing ve durdurma yarışlarının gerçek servis testini gerektirir.
Uygulama bir güvenlik sandbox'ı değildir: veritabanına/koda tam erişen biri
kuralları değiştirebilir. Canlıda izin servisi ve ayrı güvenlik sınırı gerekir.
Üretim brief'ine verilen alanların içeriğinin de ayıklanması gerekir;
örnek yalnız üst alanları daraltır, hassas bilgiyi anlamsal olarak saptamaz.

SONRAKİ KABUL KAPISI
Her gerçek marka için hesap, alıcı, proje, onaycı ve kapsam yetkileri
tanımlanmalı; gerçek geçmişten zaman sınırı korunmuş örnekler hazırlanmalı;
tanımlanmış test alıcısı ve deneme Basecamp alanında uçtan uca test yapılmalı.
Bu kuralların başarı sonucu canlıda hatasızlık garantisi değildir.
