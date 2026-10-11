HAMLE — İLK MÜŞTERİ TEMSİLCİSİ İÇ AKIŞININ BULUT REÇETESİ
10 Ekim 2026; öneri. Bu dosya kaynak oluşturmadı, sır okumadı/taşımadı,
bulut çağrısı veya ücretli model denemesi yapmadı. Makinece okunabilir tam
kapsam cloud-launch.json dosyasındadır. 65 iş / 19 kabul / 27 puan / 0 canlı
kabul edilmiş ajan başlangıcı değişmemiştir.

İlk somut teslim:
Ahmet'in bir iç talebi -> Hamle'nin değerlendirmesi ve eksik soruları ->
yanıt taslağı ve brief -> onaylanan iç Basecamp görevi -> durum/revizyon
takibi. Hamle bu akışı Mac kapalıyken yürütebilmeli. Diğer üç ajan bunun
önkoşulu değildir. İlk dış etkiler yalnız Ahmet ve “Hamle — İç Deneme” içindir.

MEVCUT KANIT VE AÇIK İŞ
Bulutta yalnız durdurulmuş sentetik prova vardır. Gmail/Basecamp yeni Mac
kimlik kontrolleri, OpenAI model metadata erişimi ve yerel insan devralması
doğrulanmıştır. OpenAI'ye 10 USD kredi yüklendi; Paid fatura görüldü ve otomatik
yükleme kapatılarak doğrulandı. İlk müşteri verisiz Astra/max çağrısı geçti:
1251 girdi, 4064 çıktı (3338 reasoning) token. Tahmin 0.21571 USD; gerçek fatura
değildir ve 1 USD çağrı rezervi korunur. Bu yerel çağrı bulut çalışma kanıtı değildir.
Üretim bulut bağlantıları, ortak dağıtık
aksiyon/maliyet kaydı, güvenli web kontrolü ve zamanlayıcı kurulmuş değildir.

EN KÜÇÜK UYGULAMA
Tek kod tabanı ve tek imaj; bir Cloud Run Job işçi, bir küçük IAP korumalı
Cloud Run kontrol sayfası. Ortak Firestore hamle-operations veritabanı.
İşçi bağlı kimliğiyle üç Secret Manager sırrını okur. Kontrol sayfası hiçbir
Gmail/Basecamp/OpenAI anahtarını okuyamaz; yalnız kalıcı kararları işler.
Panel Ahmet'in Google girişiyle telefondan da açılır; localhost bağımlılığı
kalkar. IAP imzalı kimlik ve CSRF uygulamada da doğrulanır.

İki giriş noktası neden var: IAP'nin varsayılan Google-managed OAuth istemcisi
standart programmatic OIDC erişimini sağlamaz. İnsan panelini ve Jobs API
tetiklemesini ayırmak, özel Google giriş sistemi yazmaktan daha küçük bir
çözümdür. İşçi dışarı açık bir HTTP adresine ihtiyaç duymaz. Scheduler, OAuth
ile yalnız sabit Jobs API adresini çağırır.

Yeni adlar öneridir: hamle-runtime, hamle-control, hamle-scheduler ve
hamle-build hizmet hesapları; hamle-runtime imaj deposu. Mevcut sentetik
hesapların yetkileri genişletilmez. İndirilebilir hizmet hesabı anahtarı
oluşturulmaz. Build hesabı sır/veritabanı okuyamaz. Her rol ve kaynak JSON'da
tek tek belirtilmiştir. Firestore server IAM veritabanını sınırlar; koleksiyon
ayrımı uygulama denetimidir. Bunu sağlayıcının satır yetkisi gibi sunmayın.

BAĞLANTI TAŞIMA — İKİ FARKLI DURUM
Gmail helper'ın MacKeychainStore.load/save ve validate_record yöntemleri
geçerli Hamle kimliği, tam readonly/send kapsamı ve refresh record yapısını
sağlıyor. Buna bulut secret store adaptörü ve bellekte kalan sessiz aktarım
eklenmeli. Keychain record konsola, dosyaya, imaja veya sohbete basılmaz.
Yeni cloud kimlik/yenileme kanıtı ayrıca alınır. Önceden ekran görüntüsüne
giren istemci sırrının kontrollü yenilenmesi hâlâ açık; rastgele sır silinmez.

Basecamp'in kurulu resmî CLI v0.13.0 belgesi, dönen refresh token'ın bağımsız
kopyalarının birbirini oturumdan çıkarabildiğini açıkça belirtiyor. Mac'teki
OAuth record'u buluta kopyalayıp ikisini birden kullanmayın. “auth token”
access token verir; kalıcı, güvenli export hazırlığı değildir. En kısa yol,
hesap sunuyorsa Hamle'ye ait ayrı PAT ve Secret Manager'dır. PAT olanağı
henüz kontrol edilmedi. Yoksa buluta ayrı login, paylaşılan dönen record,
tek yenileme sahibi ve belirsiz yenilemede durdurma gerekir. Kendi adına
başka Basecamp agent kimliği yaratmak Hamle kişinin yerine geçmez.
Provider token kapsamı geniş olabilir; 5898865/49184053 uygulama kısıtı şarttır.

OpenAI mevcut özel anahtarı kullanılabilirliği doğrulanarak güvenli taşınır;
yenisi gereksiz yere yaratılmaz. İlk gerçek çağrı yalnız gpt-6-astra + max,
müşteri verisiz ve küçük sınırla geçti; kanıt openai-first-inference.json.
Kredi/fatura adımı tamamlandı; tekrar sorulmaz.
Model değiştirme yoktur.

KALICI AKIŞIN ZORUNLU SINIRI
Model yalnız öneri üretir; doğrudan mail veya Basecamp yazamaz. İnsan onayı
tam alıcı/yanıt/görev içeriğine, kaynak parmak izine ve kontrol nesline bağlıdır.
Her dış etkiden önce kaynak, canlı kimlik, proje üyeleri/görünürlük, yetki,
bütçe ve durdurma tekrar kontrol edilir. Firestore transaction sadece niyeti
tekilleştirir ve dispatch_started kaydeder; harici API transaction içinde
çağrılmaz. Sonuç kaydı ve geri okuma yapılır. Belirsiz gönderim otomatik
tekrarlanmaz. Stop bekleyenleri iptal eder; sağlayıcıya zaten ulaşmış çağrıyı
geri alacağı iddia edilmez. UI bunu “işlem sürüyor/sonuç belirsiz” diye gösterir.

E04: Hamle'ye gelmeyen CC çıkarma mailini Hamle'nin kutusunu yeniden okumak
göstermez. Bulut Durdur düğmesi bunun otomatik çözümü değildir. Müşteri kapsamı,
görünür durdurma/yeniden yetkilendirme ve gecikmiş aksiyon kuralı kabul edilene
kadar gölge/inceleme düzeyinde kalır. Ahmet'in kutusuna gizli erişim yoktur.

ZAMAN VE PARA
Hafta içi 21 tur için iki cron: 08:30 tek tur; 09:00–18:30 yarım saatlik
20 tur. Saat dilimi Europe/Istanbul. Sabah özeti ilk başarılı 08:30 turundan
sonra ayrı tekilleştirilmiş aksiyondur. Tatil takvimi henüz yetkili değildir.
Cron'lar başlangıçta kapalı kalır. Tekrarlı tetiklemeyi kalıcı slot kaydı tutar.

Ortak sınır 50 USD; yeni kayıt 10 USD OpenAI ön ödemesi + 3 USD henüz
mutabık olmayan Google rezervi + yeni 5 USD bulut kurulum rezervi = 18 USD bağlı,
32 USD serbest bakiye gösterir. Yeni rezerv: cloud-account-manager-foundation-20261010-v1.
OpenAI otomatik kredi yüklemesi kapalıdır. Ön ödemeden tüketilen token gideri
ortak toplama ikinci kez eklenmez; kalan kredi/çağrı rezervi ayrıca izlenir.
Kanıtlar: openai-billing-funded.json ve shared-budget-after-openai-funding.json.
Dağıtımdan önce Google gerçek raporu tekrar okunmalı, 3 USD rezerv korunmalı
ve tüm ön ödeme/rezerv kayıtları bulut defterine sıfırlamadan taşınmalıdır.
Son tarih 12 Ekim 2026 23:59:59 İstanbul. Her yeni aksiyon saat kontrolü,
son tarihten önce her iki cron'u buluttan durduracak doğrulanmış mekanizma ve
kalıcı kaynak giderlerinin kapatma/temizlik planı birlikte gereklidir. Bu
sonlandırma henüz uygulanmadı; bu nedenle gözetimsiz cron açılmamalıdır.
Alarm, uygulama saati veya durdurulmuş job bütün sağlayıcı faturasını kesmez.

AHMET'TEN YALNIZ GERÇEKTEN GEREKENLER
1. Yeni bulut erişimi, dört dar hesap, üç bağlantının Secret Manager'a taşınması
   ve yalnız Ahmet'e açık kontrol paneli için açık onay alındı; tekrar sorulmaz.
2. Gerekirse resmî Basecamp sayfasında Hamle'nin ayrı bulut erişimini tamamlamak.
   PAT/token/şifreyi sohbete yazmak veya göstermek istenmez.
3. OpenAI kredi/ödeme adımı tamamlandı; bu adım için yeni işlem istenmiyor.
4. Görünen tek iç işin gerçek yanıt/görevini onaylamak; bağımsız çalışma ve
   devralmanın gerçek sonucunu görmek. Eski test maili/yorum tekrar üretilmez.
Müşteri açılışı geldiğinde marka/proje/kişi/hizmet/revizyon ve E04 yetki
alanları tamamlanır; bunlar iç akışı geliştirmeyi şimdi durdurmaz.

UYGULAMA SIRASI
Önce adapters + Firestore ledger/control/budget + onaylanan çıktı sözleşmesi.
Sonra dar bulut izinleri ve sır aktarımı; kapalı dağıtım; yalnız okuma kimlik
kontrolleri; IAP insan girişi/durdurma; küçük Astra çağrısı; tek tam iç iş.
İşin Mac kapalıyken takip edildiğinin kanıtından sonra süre/bütçe korumalı
iç zamanlama. Ardından geçmiş taleplerde gölge ve kabul edilen rutin kapsam.

RESMÎ KAYNAKLAR (10 Ekim 2026 kontrol edildi)
https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run
https://docs.cloud.google.com/iap/docs/authentication-howto
https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule
https://docs.cloud.google.com/firestore/native/docs/security/iam
https://docs.cloud.google.com/secret-manager/docs/best-practices
https://docs.cloud.google.com/secret-manager/docs/add-secret-version
https://raw.githubusercontent.com/basecamp/basecamp-cli/v0.13.0/README.md
