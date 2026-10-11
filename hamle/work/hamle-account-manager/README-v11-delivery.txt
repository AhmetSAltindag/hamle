Hamle v1.1 — Basecamp üretim, kalite ve teslim koordinatörü
========================================================
Durum: Yeni ve yalıtılmış geliştirme modülü. V7 bulut işçisi, canlı zamanlayıcı,
kontrol ekranı veya müşteri gönderimi bu modülü henüz kullanmıyor. Bu dosyalar
buluta yüklenmedi; gerçek Gmail/Basecamp yazımı ve ücretli model çağrısı yapılmadı.

Dosyalar
- v11_delivery.py: deterministic DeliveryCoordinator, preflight_intent,
  record_outcome ve DeliveryAdapter protokolü.
- test_v11_delivery.py: 38 sentetik/offline test.
- test_v11_queue_delivery.py: 4 sentetik kuyruk + koordinatör + sahte sağlayıcı
  entegrasyon testi. Test sayısı bu sürümün kaydıdır.

Akış
1. Güvenilir adapter, doğru işe/projeye/göreve ait güncel Basecamp durumunu ve
   sınırlı, artan sıra numaralı yorum/olay sayfasını okur. İmleç son sayfaya
   ulaşmadan aksiyon üretilmez. İmleç değişmese bile güncel görev, dosya, yetki
   ve devralma durumu her turda tekrar okunur.
2. Tamamlandı işareti ya da 'hazır' yazan bir yorum tek başına teslim değildir.
   Doğrulanmış görev sorumlusunun açık üretim teslim kaydı ve tam dosya hash'i
   gerekir. Adapter bu kayıtları yorum metninden/model yanıtından uyduramaz.
3. Brief'in istediği dosyalar, tür, boyut ve somut ölçütler kontrol edilir.
   Her dosyanın ölçüm kanıtı o dosyanın SHA-256 değerine bağlıdır.
4. Kalite raporu kabul edilmiş marka QA politikası, güncel onaylı varlıklar,
   brief, çıktı sürümü, dosya manifesti, kaynak mail ve alıcılarla bağlıdır.
   Gerçek kalite eşiği kod tarafından icat edilmez. Yapılandırılmış semantik ve
   olgusal kontrollerin hepsi geçmelidir. Üst düzey 'accepted' model cevabı,
   başarısız olgusal kontrolün yerine geçemez. Dosya/brief değişirse eski QA ve
   üretici serbest bırakma kaydı geçersizdir.
5. Eksik varsa yalnız müşteri görünürlüğü kapalı Basecamp proje/görevine iç
   revizyon notu niyeti üretilebilir. Bu not müşteri mail zarfına konmaz.
6. Kabul edilmiş çıktı için yetkili üreticinin son teslim serbest bırakma kaydı
   gerekir; tam müşteri mail zarfının hash'ine de bağlıdır. Bu kişinin kim olduğu
   doğrulanmış marka hizmet kapsamından gelir. Ahmet'in her teslimi onaylaması
   şeklinde sabit bir gereklilik eklenmemiştir.
7. Gönderim niyeti mevcut autonomy_policy.authorize_action kapısından geçer:
   gerçek son gelen mailin To/CC'si, izinli kişiler, sözleşme/revizyon sınırı,
   kaynak ve kontrol tazeliği, açık eskalasyonlar ve insan devralması denetlenir.
   Gecikmiş iş için özel güncel yetki kanıtı yoksa işlem tutulur. Hamle'nin hiç
   almadığı CC'den çıkarılma mailini posta kutusundan görme sorunu çözülmüş değildir.
8. Niyetin çıkması gönderim değildir. Yürütücü, işin kalıcı kuyruk kilidini alır;
   hemen önce preflight_intent ile tüm canlı kanıtı tekrar toplar; bütçeyi ayrıca
   kontrol eder ve begin_write kaydını kalıcılaştırır; yalnız bir sağlayıcı
   denemesi yapar; gerçek cevabı tam içerik/hash ile geri okuyup kaydeder.
9. Belirsiz sonuç otomatik tekrarlanmaz. Aynı doğrulanmış çıktı yeniden gönderilmez.
   Tekrar uzlaştırma ancak doğrulanmış sağlayıcı kanıtıyla yapılabilir.

Durum sözleşmesi
- awaiting_production: henüz doğrulanmış üretim teslimi yok.
- changes_requested: düzeltme gerekiyor; not niyeti/kanıtı ayrıca izlenir.
- ready_qa: sürüm var; kabul edilmiş QA kanıtı bekleniyor.
- ready_delivery: QA geçti; sebep alanı üretici kaydı mı, yürütücü mü bekleniyor
  açıkça söyler. Bu durum tek başına 'müşteriye gönderildi' değildir.
- held: yetki, kanıt, kapsam, insan devralması veya belirsiz sonuç nedeniyle durdu.
- delivered: tam sağlayıcı geri okuması kayıtlı.
state.status güncel gözlem/aksiyon evresidir. last_confirmed_delivery ise değişmez
geçmiş teslim kanıtıdır; sonraki turda dosya/QA görünmez olsa bile silinmez.
Kontrol ekranı son gerçek teslimi bu alanla ayrıca göstermeli; yeni gözlemin
awaiting_production olması geçmişte gönderilen teslimi yok saymamalıdır.
state tek iş içindir ve kuyruk kilidi altında kalıcı saklanmalıdır; modülün kendi
veritabanı yoktur. Kuyruğun records alanına sınırsız mail veya dosya gömülmemeli.

Teslim biçimi sınırı
Mevcut authorize_action sözleşmesi tek aktarım dosyası kabul eder. Birden fazla
zorunlu dosya varsa güvenilir adapter tarafından oluşturulmuş, politika açısından
onaylı tek paket + içindekilerin doğrulanmış manifesti gerekir. Kod kendiliğinden
ZIP üretmez ya da birden çok mail/ek gönderimini atomik tamamlanmış saymaz.
Sadece kaynak URL'ye bakarak dosya tamam/kaliteli sayılmaz. Adapter gerçek baytları
hash'lemeli, URL yönlendirmesini/erişimini sınırlandırmalı ve içerik analizi yapmalıdır.

Adapter güven sınırı
read_basecamp(work, after_cursor) -> snapshot:
  binding (iş+proje+görev eşlemesi), observed_at/evidence_ref, previous_cursor,
  cursor/has_more, artan source_order'lı events, task_fingerprint, assignee_ids,
  artifact + doğrulanmış deliverables manifesti, producer_submission,
  qa, release, customer_subject, customer_body.
read_authority(work) -> scope + request:
  doğrulanmış sözleşme, güncel gerçek kaynak mail, devralma durumu, mevcut iş
  yetkisi ve gerektiğinde action/artifact/prepared_action'a bağlı güncel yetki.
Adapter canlı sağlayıcı kimliği/üyelik/görünürlük/kişi eşlemesini kendisi doğrular.
Sadece 'verified': true alanını model çıktısından kopyalamak adapter değildir.
Müşteri email/CC korunur; mail gövdesinin tam hali son üretici serbest bırakmasına
bağlıdır. Dosyanın olumlu QA'sı ayrıca maildeki yeni iddiaları onaylamaz; serbest
bırakma adapterinin mail metnini de doğrulaması gerekir.

Kuyruk bağlantısı
v11_work_queue.QueueStore bağlaması, global/iş nesli ve task_id aynı gerçek
kaynaklardan gelmelidir. Offline entegrasyon testi bunu örnekler:
  enqueue_event(source_kind='basecamp', immutable observation fingerprint)
  claim_next(work) -> coordinator.poll -> coordinator.preflight_intent
  prepared_binding = intent.authority_request.work_binding + queue event fingerprint
  begin_write(claim, kind, payload hash, action_key=intent.action_id, prepared_binding)
  tek sağlayıcı denemesi -> tam readback -> confirm_write -> record_outcome
  complete_event
prepared_binding'in iş alanları sadece kuyruktan yeniden kopyalanmamalı; hazırlanmış
niyetten gelmeli ki yanlış iş niyeti başka işin kilidine bağlanamasın. Başlatma ile
sağlayıcı çağrısı arasındaki insan devralması için üretim yürütücüsü son kontrol
sınırını da uygular. Zaten yola çıkmış sağlayıcı POST'unu geri alma garantisi yoktur.

Doğrulama
../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_delivery test_v11_queue_delivery -v
İki iş provasının biri hatalı biçim/ölçüt sonucu iç revizyona, diğeri kabul edilmiş
çıktıyı doğru mail zincirine teslim niyetine gider. Sahte sağlayıcıda toplam iki
kesin yazım; ikinci taramada sıfır ek yazım. Bir işte belirsiz sonuç/devralma diğer
bağımsız işi durdurmaz. Bu gerçek müşteri veya Cloud kabul kanıtı değildir.

Canlıya geçmeden kalanlar
Marka/hizmet/kişi/sorumlu/teslim/QA/revizyon kayıtları; gerçek byte/format/semantik
QA adapteri; üretici serbest bırakma ve görünür iş devralma arayüzü; gecikmiş yetki
mekanizması; mail+Basecamp çoklu giriş entegrasyonu; kalıcı delivery state; bütçe
kontrolü; canlı okuyucu/yazıcı/denetim ekranı bağlantısı; iç uçtan uca ve gölge kabul.
