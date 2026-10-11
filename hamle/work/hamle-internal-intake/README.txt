HAMLE — ÇEVRİMDIŞI İÇ TALEP İNCELEME KUTUSU

intake.py çekirdeği mail/API okumaz, yazmaz ve model çağırmaz. Yalnız önceden
yakalanmış normalizer metadata veya sentetik fixture kabul eder. Canlı müşteri
işi, otonom ajan, yetki verme veya gönderim kuyruğu değildir.

Sözleşme: mail_ingest.normalize_thread çıktısı observation; ayrı, güvenilir
çağırandan Capture(source, mailbox, observed_at, complete_visible_snapshot,
fingerprint); sonra ReviewInbox.review(observation, capture=..., now=...).
Capture alanları mail/ek içeriğinden doldurulmaz. snapshot_fingerprint yalnız
metadata bağlar; kimlik doğrulama veya dijital imza sağlamaz. Yakalanmış bir
dosyanın gerçek sağlayıcı cevabı olduğunu bu modül ispatlayamaz.

Dar pilot yalnız bilinen Ahmet adresinden gerçek Hamle To/CC alanına gelen
talepleri insan incelemesine alır. Şirket domaini tek başına yetki değildir.
Normalleştiricinin doğrulanmış doğrudan başlık, katılım ve yön alanları birlikte
kontrol edilir. Sıralama belirsizse kapalı kalır. En son görünür gelen mesaj
otomatikse veya Hamle To/CC'de yoksa eski uygun maile geri düşülmez. Daha yeni
kendi yanıtı varsa eski talep otomatik yeniden inceleme işi yapılmaz.
BCC, yönlendirilmiş/alıntı metin, gövde, ek ve action_eligible talimatları yetki
kazandırmaz. İnceleme çıktısında gövde/ek/başlık metni kopyalanmaz.

ReviewInbox.stop(actor=Ahmet, takeover=True/False) bekleyenleri boşaltır,
kontrol neslini yükseltir. resume_review yalnız incelemeyi açar. Bu metodlar
insanı kendi başına doğrulamaz; ileride kimlik doğrulamalı arayüz gerekir.
300 saniyeyi aşmış yakalama held olur. renew_delayed_review, Ahmet'in ayrı
insan olayıyla ve aynı metadata parmak izine bağlı olarak yalnız inceleme için
yenileme kaydeder. Onay eski nesilde, farklı snapshot'ta veya 300 saniyeden
eskiyse geçmez. Bu 300 saniye yerel prova eşiğidir, operasyon politikası değildir.
Her pending() çağrısı süreyi yeniden kontrol eder. Kuyruk bellektedir; yeniden
başlatmada saklanmaz. Aynı oturumda daha yeni veya dışlayan bir snapshot
görüldükten sonra eski snapshot tekrar verilerek eski iş açılamaz. Değişen veya
eksilen geçmiş metadata insan incelemesi gerektirir; Gmail silme/değişme olayını
bu yerel katman kendiliğinden yorumlamaz. Yalnız kontrol edilen metadata'nın
ayrı kopyası tutulur; mail gövdesi ve ekler inceleme kutusuna alınmaz.
Mevcut SyntheticLedger okunup değerlendirildi fakat bu
modül ona gönderim/dispatch niyeti eklemez.

E04 AÇIK: internalDate sadece yakalanan görünür mesajların sırasıdır. Hamle'ye
ulaşmayan CC'den çıkarma görülmez; tam dünyadaki en son ilgili mail ispatlanmaz.
İnsan yenilemesi bu eksikliği çözmez. Çıktı her zaman action_eligible=False;
üretim/brief/Basecamp/teslim aksiyonları için kullanılamaz. Her gerçek dış
işlem öncesi güncel yetki kontrolü ve kalıcı devralma hâlâ ayrı iştir.

Test (bu klasörde):
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest discover -p 'test_*.py' -v
Testler özgün normalizer'ı yalnız sentetik cevaplar üzerinde çağırır; gerçek
Gmail taşıması çağrılmaz. Wrapper testlerinde özgün identity/profile/scope ve
tek-zincir okuma yardımcıları sahte Keychain/transport ile sınanır. Ağ/sırlar
kullanılmaz.
65 iş / 19 kabul / 27 puan başlangıcı bu hazırlıkla değişmez.

TEK İÇ MAİL İÇİN AÇIKÇA ÇALIŞTIRILAN SALT OKUMA WRAPPER'I
capture_internal_test.py ancak --read-internal-test ile gerçek Gmail okur:
  ../hamle-gmail-connector/.venv/bin/python -B capture_internal_test.py --read-internal-test
Bayrak olmadan Keychain/API kullanılmaz. Varsayılan/arka plan çalışma yoktur.
Sabit konu: HAMLE-IC-PROVA-20261010-YENIMAC-01
Sabit gönderen: ahmet@gamechangers.com.tr
Doğrudan To veya CC: hamle@gamechangers.com.tr
Yalnız mevcut Keychain ve özgün verified_gmail içindeki tam gmail.readonly +
gmail.send kapsam/kimlik kontrolü kullanılır; bu wrapper gönderim yapmaz.

Arama en çok iki thread kimliği alır; sıfır, çoklu veya sayfalı sonuç kapalı
kalır. Eski test zinciri 1a12270588d30c79 kesin reddedilir. Arama yetki değildir:
önce gerçek Subject metadata'sında tam eşleşme aranır, ardından özgün
tek-zincir okuyucu body_message_id=None ile çağrılır. İki okuma arasında mesaj
kimlikleri veya zamanları değişmişse reddedilir. Son gelenin konusu ayrıca tam
eşleşmelidir; eski mailin konusu, alıntı metin veya Re: eşleşmesi yeterli değildir.
Son gelenin gerçek From/To/CC/self/auto kontrolü intake katmanında tekrar yapılır.

Özgün okuyucu Gmail format=full cevabını bellekte alır, body izni verilmediğinden
gövde çözümlemez/çıktıya vermez. Wrapper ham başlık, gövde, ek adı veya sır
yazdırmaz. Ek API'si, mark-read, send, Basecamp/model çağrısı veya zamanlayıcı yoktur.
Çıktı yalnız güvenli durum ve review metadata'sıdır; hiçbir gerçek aksiyon açmaz.
Bu wrapper'ın gerçek Gmail çağrısı geliştirme testlerinde yapılmadı.

10 EKİM YENİ MAC CANLI İÇ OKUMA KANITI
Kullanıcının gönderdiği sabit iç deneme konusu gerçek Gmail'den salt okunarak
review_ready sonucu verdi. Kanıt: outputs/new-mac-2026-10-10/internal-intake-live-read.json.
Çıktıdaki capture_observed_at, gerçek okuma tamamlandıktan sonraki yerel zamanı
taşır. Bu zaman mail içeriğinden alınmaz. Sonraki kalıcı inceleme provası ayrı
hamle-review-control modülündedir; okuma öncesi başlatılan ticket ile sonucu
kalıcı kontrol nesline bağlar. Eski kanıt dosyasına yeni zaman yazıp taze okuma
gibi içe aktarmak kabul edilmez. İki modül de dış işlem yetkisi vermez.
