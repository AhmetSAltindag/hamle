Hamle — Ahmet Google kimliğiyle açılan bulut kontrol yüzeyi

Giriş noktası WSGI factory: cloud_control:create_app()
Örnek deployment command (root paketinde gunicorn sürümü sabitlenmeli):
  gunicorn --workers 1 --threads 4 --bind :$PORT 'cloud_control:create_app()'
İlk kurulumda Cloud Run max-instances=1 ve gunicorn workers=1 kullanılır.
Çok worker/instance bellekteki panel oturumunu paylaşmaz; yanlış kimliğe
yetki vermek yerine oturum yenileme ister. Restart da sayfa yenileme gerektirir.

Zorunlu ortam ayarları:
IAP_AUDIENCE=/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME
PUBLIC_ORIGIN=https://tam-hizmet-hostu.run.app (sonunda / bulunmaz)
HAMLE_ALLOW_FIRESTORE_OPERATIONS=yes
İsteğe bağlı IAP_ALLOWED_SUBJECT: ayrıca sabit IAP sub eşleşmesi istenirse.
Eksik audience/origin durumunda factory başlamaz. Firestore sabit olarak
gamechangers-hamle / hamle-operations kullanır; yeni kaydı otomatik açmaz.

Kimlik:
- Her sayfa, durum okuma ve POST için X-Goog-IAP-JWT-Assertion gerekir.
- JWT alg ES256, key id, Google public-key imzası, exact audience,
  https://cloud.google.com/iap issuer, iat/exp (30sn tolerans, en fazla
  660sn ömür), exact email ahmet@gamechangers.com.tr ve nonempty stable
  subject doğrulanır. Düz email/user header kimlik kanıtı değildir.
- Doğrulama google.oauth2.id_token.verify_token ve
  google.auth.transport.requests.Request kullanır. Sertifika adresi sabit:
  https://www.gstatic.com/iap/verify/public_key (10sn timeout).
- Subject imzalı JWT'den gelir; panel çerezi ve CSRF oturumu ona bağlıdır.
- __Host- çerezi Secure + HttpOnly + SameSite=Strict, Path=/; 30dk ömür.
  Rastgele session id HMAC imzalıdır; nonce map yalnız süreç belleğindedir.
- Cloud Run IAP etkin kalmalı; Cloud Run allUsers/allAuthenticatedUsers
  invoker açılmamalı. Yalnız onaylı IAP service-agent invoker, Ahmet IAP
  erişimi. Uygulama kontrolü altyapı IAM denetiminin yerine geçmez.

Kontrol:
- GET hiçbir kontrol mutasyonu yapmaz.
- POST yalnız /api/control, exact PUBLIC_ORIGIN + Host, CSRF ve JSON ile.
- Durdur / Ben devralıyorum / Yalnız incelemeyi yeniden aç işlemleri
  shared Firestore store'a yazılır. Eski iptalleri yeniden canlandırmaz.
- Tam çıktı onayı son görüntülenen proposal id/hash/generation/purpose'a bağlıdır;
  store tekrar transaction içinde doğrular. UI harici API çağırmaz.
- POST approve için purpose açıkça zorunludur. Rutin onay routine_execution;
  complaint_strategy + cansu_and_ahmet için ayrı internal_coordination_only.
  Amaçsız veya farklı amaca çevrilmiş istek reddedilir.
- “Yalnız iç koordinasyonu onayla” yalnız ekranda gösterilen kesin Kime/CC
  alıcılarına yanıt ve özel Hamle iç görev işlemi içindir.
  Strateji/üretim/bütçe/teslim kararı yetkisi vermez.
  Onayda escalation_status=open, decision_authorized=false ve
  production_authorized=false korunur. Cansu/Özge danışması doğrulanmaz.
  Mevcut başka amaçlı onay yükseltilmez; tekrar onay TTL zamanını yenilemez.
- Gönderim/teslim ancak sağlayıcı receipt/readback durumuyla ayrı gösterilir.
  Onay verilmesi gönderim kanıtı değildir. Belirsiz sonuç otomatik tekrarlanmaz.
- Stop önceden sağlayıcıya ulaşmış isteği geri alamaz; unknown/accepted
  durumları iptal edilmiş gibi gösterilmez.
- Model/Gmail/Basecamp sırları kontrol hizmetine verilmez. Firestore ADC
  yalnız kontrolün onaylanmış hizmet kimliği üzerinden alınır.

Mevcut işe takip önerisi — yerel hazırlık, canlı kabul yerine geçmez:
- İki kesin zarf desteklenir: ilk iş scope/analysis/reply/task/source;
  takip scope/analysis/work/comment/reply/source. Takip zarfında task yoktur;
  yeni bir görev oluşturuluyormuş gibi gösterilmez.
- work: work_id, thread_id, task_id, account_id, project_id, version=1,
  escalation_status ve escalation_owner. comment: account_id, project_id,
  task_id, todo_parent_id, todo_parent_type, body ve task_fingerprint.
  Hesap/proje/görev ve thread eşleşmesi; 64hex durum izi ve tam öneri hash'i
  doğrulanır. Görev bağlantısı sabit Basecamp iç proje adresinden üretilir.
- “Mevcut göreve eklenecek takip notu” tam yorum gövdesini ve aynı görev
  bağlantısını gösterir. Tüm Kime/CC alıcıları onaydan önce görünürdür.
  recipient_policy doğrulaması yalnız Kime=Ahmet ve mevcut iç kapsamda
  doğrulanmış Cansu CC'sine izin verir. Başka alıcı, tekrar veya başlık
  enjeksiyonu kapalı kalır. Cansu'nun CC'de olması görüş/onay kanıtı değildir.
- İşin open + cansu_and_ahmet yönlendirmesi; sonraki model analizi rutin,
  sosyal plan veya belirsiz olsa da internal_coordination_only olarak kalır.
  Ticari karar her durumda bu panel onayına kapalıdır. Cansu/Özge danışılmış
  varsayılmaz; koordinasyon onayı insan değerlendirmesini kapatmaz.
  Önceden not_required işte yeni insan yönlendirmesi tespit edilirse onay
  kapalı kalır; bu sürüm kalıcı sorumluluğu kendiliğinden değiştirmez.
- public_state.works için yalnız iş bağlantısı, revision_count,
  pending_escalation ve latest_observation içinden status/completed/due_on/
  observed_at tarayıcıya verilir. Gözlem doğru iş kimliği ve identity_verified
  taşır; gözlem yoksa “Henüz doğrulanmadı” gösterilir. Son kontrol İstanbul
  saatindedir. Ham görev açıklaması, event/actor/lease alanları aktarılmaz.
  “Doğrulanmış takip turu” sayısı ancak yorum ve yanıtın ikisi de geri okuma
  ile doğrulandığında artar; yalnız yorum kabulü tamamlanmış tur sayılmaz.
- Kartın durumu son başarılı okumadır; sürekli izleme veya tamamlanmış
  revizyon üretimi kanıtı değildir. Bu yüzey iş çalıştırmaz veya dağıtım yapmaz.

Kaynak (10 Ekim 2026 okundu):
https://docs.cloud.google.com/iap/docs/signed-headers-howto

Yerel testler dış çağrı yapmaz; fake IAP verifier ve fake store kullanır.
Gerçek ES256 imza doğrulaması geçici belleki test anahtarı ve yerel public-key
yanıtı ile sınanır; değiştirilen imza/payload reddedilir. İmza kitaplığına
verilen certs_url/audience parametreleri ayrıca test edilir. Gerçek
OperationsStore, seri işlem uygulayan MemoryBackend ile onay/devralma ve
yeniden açmada eski iptalin kapalı kalması için entegrasyon testinde kullanılır.
Test bağımlılıkları google-auth, requests ve cryptography; prod bağımlılıkları
root imaj tarifinde sabitlenmelidir. Test komutu:
  python -m unittest discover -s . -p test_cloud_control.py -v

Tarihsel canlı kanıt — 2026-10-10T02:47:51.267860+03:00 (İstanbul)
V2 Gmail/Basecamp kimlik okuması geçti. Gerçek iç mailin cloud Astra/max
incelemesi hamle-account-manager-6p2px içinde tamamlandı; tek bulut çağrısı,
onay bekleyen complaint_strategy önerisi, 0 iş yazması. Yerel sentetik
çağrıyla toplam gerçek model çağrısı 2. Bulut çağrısının 0,24662 USD token
maliyet tahmini kesin fatura değildir ve mevcut ön ödemeye yeniden eklenmez.
Kanıt: ../../outputs/new-mac-2026-10-10/cloud-first-real-inference.json ve
cloud-execution-v2-internal-review.json. V3 dar koordinasyon kontrolü yerelde
hazırdır, derlemesi sürmektedir; canlı dağıtımı henüz doğrulanmamıştır.
Kullanıcının sonraki adımı v3 hazır olduğunda tam çıktıları incelemek ve
uygunsa “Yalnız iç koordinasyonu onayla” düğmesini kullanmaktır.
Zamanlayıcı ve müşteri akışları kapalıdır; 19/65, 27/100 ve 0/4 kabul tabanı
değişmemiştir. Başarısız v1 çalıştırma kanıtı tarihsel olarak korunur.
Yukarıdaki V3 öncesi paragraf tarihsel durumdur. İlk iç koordinasyonun daha
sonraki V5 kabul kanıtı ../../outputs/new-mac-2026-10-10/
cloud-first-internal-completion.json içindedir. Buradaki takip ekranı değişikliği
o önceki kabule yeni takip/revizyon veya sürekli çalışma kabulü eklemez.
