HAMLE — 12 EKİM 2026 İÇ PİLOT ZAMANLAMA PLANI
Hazırlanma: 10 Ekim 2026. Bu belge ve scheduler-plan-20261012.json yalnız yerel plandır; zamanlayıcı kurulmuş veya etkinleştirilmiş sayılmaz.

Kapsam
Gamechangers iç denemesindeki mevcut zincir ve mevcut Basecamp görevi, 12 Ekim Pazartesi İstanbul saatinde 08.30–18.30 dahil 15 dakikada bir kontrol edilir: 41 planlanmış tur. İşleyici scheduled_review kipinde öneri üretir; mail/yorum gönderimini kendiliğinden açmaz. Müşteri kapsamı kapalı kalır. Yeni hesap, sır veya IAM erişim genişletmesi yoktur.

Üç kesin takvim
hamle-pilot-20261012-early: 30,45 8 12 10 *       (2 tur)
hamle-pilot-20261012-day:   */15 9-17 12 10 *     (36 tur)
hamle-pilot-20261012-late:  0,15,30 18 12 10 *    (3 tur)
Saat dilimi: Europe/Istanbul. Haftanın günü alanı * kalır. Bu alana MON eklemek gün/hafta OR kuralı nedeniyle Ekim'deki diğer pazartesileri de açar. Tek */15 8-18 ifadesi 44 çağrı çıkarır. Cron yıl ve bitiş tarihi taşımaz; bu üç kaynak silinmezse sonraki yıllarda 12 Ekim tekrar denemesi yapar. 2026 dışındaki Cloud Run başlangıcını ayrı IAM koşulu reddedecektir.
Kaynak: https://docs.cloud.google.com/scheduler/docs/configuring/cron-job-schedules

Mevcut hedef ve izin daraltması
Proje gamechangers-hamle; bölge europe-west1; Cloud Run job hamle-account-manager. Çağıran mevcut kimlik hamle-scheduler@gamechangers-hamle.iam.gserviceaccount.com. İşleyici kimliği hamle-runtime olarak kalır. Son doğrulanmış imaj korunur; argümanlar python worker.py --mode scheduled_review olur. Tek görev, paralellik 1, yeniden deneme 0, süre 600 saniye; CPU 1 ve 512 MiB korunur. Varsayılan argüman değiştirme komutu JSON'dadır. Scheduler istek gövdesi {} olduğundan run.jobs.runWithOverrides izni gerekmez. Betik eski 300 saniyelik dağıtımı hazır saymaz; v7'nin 600 saniyelik doğrulanmış dağıtımı önce tamamlanmalıdır.

İş üzerindeki mevcut koşulsuz roles/run.invoker üyeliği aynı üyeye tarih koşullu üyelikle DEĞİŞTİRİLİR; koşulsuz üyelik yanında bırakılmaz. Koşul başlığı hamle_scheduler_20261012_only:
request.time >= timestamp("2026-10-12T05:30:00Z") && request.time < timestamp("2026-10-12T15:35:00Z")
Bu, İstanbul 08.30 dahil–18.35 hariç aralığıdır. Son tur 18.30'dur; 18.35'ten önce kabul edilmiş bir çalışmanın 600 saniyelik süresi ayrıca devam edebilir. Bu süre, 12 Ekim 23.59.59 harcama yetkisi sonundan erkendir.

IAM kaydı requestedPolicyVersion=3 ile taze okunur. Taze etag ve policy version 3 kullanılarak yalnız bu üye değiştirilir; diğer üyeler, koşullar ve denetim ayarları korunur. Yazı sonrası aynı v3 API'den koşul geri okunur. Eski etag tekrar kullanılmaz. Daha sonraki dağıtım betiğinin tarihsel koşulsuz invoker ekleme adımı tekrar çalıştırılmaz. Koşul reddedilir/silinirse veya başka geniş çağırma yetkisi görülürse DUR: proje geneline izin, TokenCreator veya yeni hesap ekleme yok.
Kaynaklar: https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/setIamPolicy
https://docs.cloud.google.com/run/docs/reference/rest/Shared.Types/Policy

Hazırlama sırası: önce etkisiz kaynak, sonra duraklatılmış gerçek takvim
1. İlk pilot saatinden önce çalışılır; devam eden Cloud Run işi bulunmaz. Güncel dağıtım ve bütçe geri okunur.
2. İşleyici varsayılanı güncellenir ve sınırları geri okunur; ardından mevcut invoker daraltılır ve koşul doğrulanır.
3. Her Scheduler kaynağı önce 0 0 1 1 * takvimiyle oluşturulur. Bu geçici 1 Ocak takvimi IAM penceresine denk gelmez. Ardından hemen pause çağrısı ve PAUSED geri okuması gerekir. Ancak bundan sonra yalnız schedule alanı yukarıdaki gerçek takvimle değiştirilir; kaynak PAUSED kalmalıdır. Hazırlık yarıda kalsa bile 12 Ekim takvimi kendiliğinden açılmaz.
4. Aynı isimde beklenmeyen kaynak varsa üzerine yazılmaz. Sadece bu planın bütün alanları eşleşen kaynağı güvenli biçimde devam ettirilebilir.
API'de state yalnız çıktıdır; create gövdesine PAUSED yazıp atomik duraklatma varmış gibi davranılmaz. HTTP hedef Google Run API'sidir, POST {} ve OAuth cloud-platform kullanır. retryCount=0, maxRetryDuration=0s, attemptDeadline=30s. JSON tam REST alanlarını içerir.
Kaynak: https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs

Etkinleştirmeden önce gerçek ret kanıtı
Mevcut koşul penceresi DIŞINDAYKEN üç kaynak da PAUSED olmalıdır. Mevcut yönetici, birinin Scheduler jobs.run metodunu yalnız bir kez çağırır. Bu yöntem Scheduler'ın kendi OAuth kimliğiyle hedefe gitmesini sağlar; yeni hizmet hesabı taklit izni gerekmez. Kanıt üç parçadır: Scheduler AttemptFinished kaydında hedef HTTP403, Cloud Run denetim kaydında tam scheduler kimliğiyle PERMISSION_DENIED, Cloud Run çalışma listesinin değişmemesi. Üç kaynak hâlâ PAUSED olmalıdır. Günlük gecikirse sadece okuma tekrarlanır; jobs.run tekrar edilmez. Sadece jobs.run API'sinin isteği kabul etmesi ret kanıtı değildir. Duraklatılmış kaynağı zorla çalıştırma sağlayıcı tarafından reddedilir veya hedef denemesi gözlenmezse sonuç HOLD olur; sessizce resume edilmez.

Cloud Run koşullu IAM'i desteklenen hizmetler arasındadır; job setIamPolicy de Policy şemasını kullanır. Ancak bu işte request.time koşulunun gerçek uygulanması henüz gözlenmedi. API kabulü, v3 geri okuma ve yukarıdaki canlı ret gerçekleşmeden hazır denmez. Bugünkü dış pencere testi gelecekteki üst sınırın fiilen yaşandığı anlamına gelmez. Üst sınır yapılandırma ve yerel sınır kontrolüyle doğrulanır; gelecek olay gözlenmiş gibi raporlanmaz.

testIamPermissions gelecekteki saati veya başka bir kimliği alamaz; Ahmet olarak yapılan çağrı scheduler kimliğini kanıtlamaz. Policy Troubleshooter request-time ile isteğe bağlı benzetim yapabilir, fakat bu gerçek çalıştırma testi değildir. Bu amaçla yeni rol veya API erişimi eklenmez.
Kaynaklar: https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs/run
https://docs.cloud.google.com/iam/docs/resource-types-with-conditional-roles?hl=en
https://docs.cloud.google.com/iam/docs/conditions-attribute-reference
https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/testIamPermissions
https://docs.cloud.google.com/sdk/gcloud/reference/policy-troubleshoot/iam

Bütçe, etkinleştirme ve kapanış
Gerçek ret kanıtından sonra, güncel ortak bütçe defterinde cloud-scheduler-pilot-20261012-v1 anahtarıyla ek 1 USD Google ihtiyat payı yalnız bir kez ayrılır. Eski 8 USD Google payları serbest bırakılmaz; Google'ın güncel faturası hâlâ doğrulanmamıştır. Başka eşzamanlı işlemler olabileceğinden eski 18/32 tutarı sabit varsayılmaz. Ortak 50 USD ve son tarih yeniden kontrol edilir. 1 USD pay gerçek fatura veya sağlayıcı düzeyinde kesin durdurma değildir. Yeni önerinin model çağrısı için işleyicide ayrılan 2 USD pay bundan ayrıdır. 41 başlangıcın 600 saniyelik nominal toplamı 24.600 vCPU-saniye ve 12.300 GiB-saniyedir; yinelenen teslimler bu hesapta yoktur.

Sonra üç iş resume edilip ENABLED, takvim, hedef, kimlik ve bir sonraki tarih geri okunur. Kısmi etkinleştirme hatasında üçünün tamamını duraklatma ve geri okuma denenir; duraklama kanıtlanamıyorsa açıkça operator_required ile durur ve işler kapalıymış gibi raporlanmaz. Pilot sonunda veya acil durdurmada bu üç iş duraklatılıp silinir; yoklukları geri okunur. Koşulsuz invoker geri getirilmez. Faturalama uzlaştırılana kadar ihtiyat payları korunur. Bu plan otomatik silici kurmaz; kapanışı yetkili yönetici yürütür.

İlk üç Scheduler işi ücretsiz kota kapsamındadır; kota faturalama hesabı başınadır ve duraklatılmış işler de sayılır. Bugünkü bölgesel boş liste, tüm faturalama hesabının envanteri değildir. Gelecekte kaynak bırakmanın tüm sağlayıcı maliyetlerini sıfırladığı iddia edilmez. Ücret kaynağı bırakmamak için pilot kaynakları kaldırılmalıdır.
Kaynak: https://cloud.google.com/scheduler/pricing

Boş tur ve tekrar güvenliği
İncelenen işleyici/kalıcı kayıt için 82 yerel test geçti. Her 15 dakikalık tur ilk beş dakikada tek kalıcı anahtarla alınır; geciken tura telafi çağrısı açılmaz. Durdurma, nesil ve tarih/bütçe kapıları korunur. Bilinen kendi gönderisi, ancak kalıcı kabul/geri okuma kaydı ile son canlı Gmail kimliği/mesajı/alıcıları örtüşürse no_new_work olur. Bu sonuç aksiyon yetkisi vermez. Aynı gelen mesajın kaydı ikinci modele dönüşmez. Yeni müşteri işi bulunmayan tur model çağırmaz; yine de Gmail/Basecamp/Firestore ve altyapı okumalarının maliyeti olabilir.

41 sayısı planlanmış kontrol yuvasıdır; Scheduler teslimi nedeniyle tam 41 kapsayıcı başlangıcı garantisi verilmez. Worker tekilleştirme iş/model tekrarını sınırlar; IAM zaman aralığı yetki bitiminden sonraki yeni kapsayıcı başlangıcını sınırlar. Sağlayıcı genelinde teknik harcama kesici hâlâ yoktur.

Bu incelemede doğrulanan mevcut durum
10 Ekim salt okuma: iş IAM'inde yalnız scheduler'ın koşulsuz roles/run.invoker üyeliği (version1); bölgede Scheduler listesi boş; Google Scheduler servis aracısı rolü mevcut; scheduler hesabında ayrıca taklit yetkisi bağlanmamış. Projede ek geniş scheduler rolü görülmedi; üst klasör/kurum politikaları taranmadı. Hiçbir bulut kaynağı bu incelemede değiştirilmedi. Canlı etkinlik ancak ayrı prepare/deny/activate kanıtları üretildikten sonra kaydedilebilir.

10 Ekim sonraki kanıt ve sınırlı pilot kabulü
İlk PAUSED RunJob isteği hedefe ulaşmadan FAILED_PRECONDITION ile reddedildi;
bu eski kayıt korunur. Ayrı tek enabled/inert-calendar denemesinde Scheduler'ın
aynı Cloud Run hedefinden HTTP403 aldığı ve yeni execution oluşmadığı görüldü.
Cloud Run DATA_WRITE principal audit kaydı yoktur; projede auditConfigs boştu.
Bu nedenle configured_oauth_service_account yalnız geri okunan yapılandırmayı
anlatır; hedefte görülmüş bir kimlik veya koşulun kesin ret nedeni iddia edilmez.

Sınırlı iç pilotta verified_target_denied alternatifi yalnız tam doğru erken
Scheduler işi/hedefi/HTTP403, güncel doğru OAuth hesabı ve conditional IAM geri
okuması, üç işin PAUSED olması ve değişmemiş execution listesi ile kabul edilir.
Kod target_principal_audit_observed=false, denial_exact_cause_not_observed=true
ve allowed_window_execution_tested=false alanlarını açıkça kaydeder. Eski
principal-audit kanıt yolu da korunur. Yeni audit izni veya üçüncü probe açılmaz.
İzin verilen pazartesi penceresinde başarılı çalışma henüz test edilmemiştir;
bu dış-pencere ret kanıtı o başarının yerine geçmez. Etkinleştirmeden önce ortak
bütçe rezervi, tarih, kontrol ve bütün güncel kaynak geri okumaları yine zorunludur.
