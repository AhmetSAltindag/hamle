HAMLE GMAIL — YEREL OAUTH BAŞLANGIÇ YARDIMCISI

Durum: Kod ve sahte servis testleri hazır. Gerçek OAuth, Gmail API veya
Keychain okuma/yazma bu geliştirme sırasında çalıştırılmadı. Yalnız manuel
Hamle → Ahmet teknik test gönderimi vardır. Genel posta taraması,
zamanlayıcı, müşteri gönderimi ve canlı iş akışı yoktur.

Sınırlar
Yalnız hamle@gamechangers.com.tr kabul edilir. Tam olarak şu iki kapsam
istenir ve gerçek izin yanıtından doğrulanır:
  https://www.googleapis.com/auth/gmail.readonly
  https://www.googleapis.com/auth/gmail.send
Eksik, fazla veya doğrulanamayan izinler reddedilir. gmail.readonly profil
ve açıkça verilen tek zincirin kontrolünde; gmail.send yalnız aşağıdaki
manuel, sabit alıcılı teknik iç bağlantı testinde kullanılır.

Token ve istemci sırrını içeren oturum kaydı yalnız macOS Keychain içinde
tutulur. Otomatik keyring seçimi veya dosyaya geri dönüş yoktur. Keychain
kilitli/erişilemezse işlem durur. Kullanıcının indirdiği Desktop client JSON
yerinde okunur; kopyalanmaz, yazdırılmaz veya paketlenmez. Dosya sahibinin
mevcut kullanıcı olması ve grup/diğer erişimlerinin kapalı olması gerekir
(0600). Dosyayı özel private-connections konumunda tutun; içeriğini sohbete
veya Git'e koymayın. OAuth için kullanıcı parolası terminalde istenmez.

Kurulum
Bu klasörde .venv, Python 3.12.14 ile hazırlandı. Bağımlılıklar yalnız
https://pypi.org/simple üzerinden yüklendi; tam çözümlenen sürümler
constraints.txt içindedir. Başka ortamda Python 3.10+ kullanın:
  python3 -m venv .venv
  .venv/bin/python -m pip install --index-url https://pypi.org/simple --no-cache-dir --only-binary=:all: -r requirements.txt

Komutlar (bu klasörden)
  .venv/bin/python hamle_gmail.py status
Bu yalnız kendi Keychain kaydını okur; API, refresh veya tarayıcı açmaz.
"stored_not_revalidated" daha önce doğrulanmış kaydı gösterir; bugün
bağlantının çalıştığı anlamına gelmez. macOS erişim istemi çıkabilir.

Kullanıcı Desktop OAuth JSON'unu özel konuma indirdikten sonra:
  .venv/bin/python -u hamle_gmail.py auth --client-secrets /ABSOLUTE/PRIVATE/PATH/gmail-desktop.json --port 0 --timeout 600
Yol örnektir; var olmayan bir dosya oluşturmayın. OAuth başlatmak ayrı
bilinçli eylemdir. Yardımcı 127.0.0.1 üzerindeki geçici portu dinler,
state kontrolü ve S256 PKCE için Google kütüphanesini kullanır. Sabit bir
port da --port 8765 gibi verilebilir. Tarayıcı kendiliğinden açılmaz.
OPEN_AUTH_URL satırındaki Google URL'sini yerel tarayıcıda açın; bu URL
yalnız o çalışan deneme için geçerlidir. Akış en çok belirtilen süreyi
bekler. Callback kodu/token asla kopyalanıp terminale yapıştırılmaz.
Hesap ve izin kontrolü geçmeden Keychain oturumu kaydedilmez. Yanlış
hesap/izin girişimi önceki geçerli Keychain kaydını değiştirmez. Yanlış
hesapla Google'da verilen izin bu araçla otomatik iptal edilmez; gerekirse
o hesabın Google bağlantı yönetiminden kullanıcı tarafından kaldırılır.
Kurulu keyring macOS backend'i mevcut kaydı silip yenisini ekleyebilir.
Kaydetme/token yenileme sırasında ikinci adım başarısız olursa eski kayıt
kaybolabilir; yardımcı keychain_write_failed ile durur ve yeniden OAuth
gerekebilir. Bu durumda da dosyaya token yazılmaz veya mail okunmaz.

Bağlantıdan sonra yalnız açıkça bilinen Hamle zincirini sınamak için:
  .venv/bin/python hamle_gmail.py read-test --thread-id KNOWN_HAMLE_THREAD_ID
İşlem önce yalnız Gmail profilinin emailAddress alanını doğrular; sonra
verilen tek zinciri minimal formatta, yalnız zincir/ileti kimlikleriyle
ister. Gövde, başlık, konu, ek, arama veya başka mail istenmez. Çıktı
sadece beklenen e-posta, kimlikler, adet ve kontrol durumudur. Gereken
token yenilemesi Keychain'e yazılır; dosyaya yazılmaz. Gmail zincir
kimlikleri hesaplar arasında aynı varsayılamaz: Ahmet'in zincir kimliği
Hamle'de bulunmazsa yardımcı aramaya genişlemez. Bu sınırlı okuma testi
gerçek To/CC yetki kapısını veya gönderim yetkisini uygulamaz/kanıtlamaz.

Manuel iç gönderim testi
  .venv/bin/python hamle_gmail.py send-test --test-id GC-HAMLE-GMAIL-20261009-01
Bu komut gerçek e-posta gönderir; kurulum, auth, status veya read-test onu
kendiliğinden çalıştırmaz. Gönderen yalnız hamle@gamechangers.com.tr,
alıcı yalnız ahmet@gamechangers.com.tr olabilir. To/CC/BCC, konu, gövde,
ek veya başka alıcı için seçenek yoktur. Sabit konu:
  HAMLE — manuel iç Gmail bağlantı testi
Sabit gövde:
  Merhaba Ahmet,

  Bu ileti, talep ettiğin Hamle Gmail bağlantısının gönderim kontrolü için manuel olarak başlatılan teknik iç testtir. Müşteri işi, görev veya onay talebi içermez; otomatik takip ya da tekrar gönderim başlatmaz.

  Hamle

Test kimliği GC-HAMLE-GMAIL-YYYYMMDD-NN biçimindedir; tarihi gerçek test
gününe göre verin. Konu ve gövde sabittir; kimlik Message-ID ve
X-Hamle-Test-ID başlıklarında yer alır. Kullanıcı adına müşteri onayı
veya müşteriden gelen bir e-postaya yanıt sayılmaz.

Gönderimden önce .send-test-attempts içinde yalnız test kimliği, sabit
gönderen/alıcı, zaman ve deneme durumu yazılır. Klasör 0700, dosya 0600
izinlidir. Atomik O_EXCL ve fsync ile oluşturulan kayıt varsa aynı ID
bir daha gönderilmez; eşzamanlı iki süreçten yalnız biri kayıt alabilir.
Bu kayıtlar token, istemci sırrı veya müşteri içeriği taşımaz. Tokenlar
yalnız Keychain içinde kalır. Başarı yanıtının kimlikleri ayrı accepted
kaydına yazılır. Bu kayıtları silmeyin veya başka kopyadan aynı testi
başlatmayın; koruma bu yerel klasörün korunmasına dayanır. Message-ID
Google tarafında tekilleştirme garantisi değildir.

API/HTTP tekrarları, 401 sonrası yeniden gönderim ve yönlendirmeler bu
komutta kapalıdır. Token gerekiyorsa önce açıkça yenilenip Keychain'e
yazılır; gönderim aktarımı sessiz token yenilemesi yapamaz. Timeout,
hata, kesinti, anlaşılmaz yanıt veya gönderim sonrası yerel kayıt hatası
send_outcome_unknown_do_not_retry sonucudur. Deneme kaydı korunur;
yeniden başlatmak veya yeni ID kullanmak yerine Ahmet/Hamle gelen ve
gönderilen kutusundan manuel doğrulama gerekir. Program bunu otomatik
aramaz. Gönderimden hemen önce süreç çökse bile kayıt kilitli kalabilir.
send_test_accepted yalnız Gmail API kabulünü bildirir; teslimi kanıtlamaz.

Testler
  .venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
Kimlik/izin reddi, Keychain zarfı ve hataları, gizli değerlerin çıktıya
sızmaması, sınırlı okuma, refresh, loopback/PKCE/state, sabit iç alıcı ve
içerik, eşzamanlı/tekrar deneme, belirsiz gönderim ve kapalı aktarım
tekrarları sınanır. Genel send komutu yoktur. API, OAuth ve Keychain
etkileri sahtedir; state/PKCE
kontrolleri resmî kütüphaneyle ağsız sınanır. Gerçek macOS Keychain izin
istemi, gerçek Google hesabı ve kullanıcı yönetim politikaları henüz
doğrulanmamıştır. Yardımcı tek süreçli bootstrap içindir; komutları eşzamanlı
çalıştırmayın. Sunucuda çalışma, kilitler ve müşteri aksiyonları ayrı iştir.

Kaynaklar (9 Ekim 2026 incelemesi)
Google Python hızlı başlangıç ve resmî istemci kütüphaneleri:
https://developers.google.com/workspace/gmail/api/quickstart/python
Desktop OAuth / loopback / PKCE:
https://developers.google.com/identity/protocols/oauth2/native-app
InstalledAppFlow referansı; kurulu 1.5.0 kaynak kodu da incelendi:
https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html
Profil ve sınırlı zincir okuma:
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/get
MIME mesajı ve sınırlı manuel gönderim:
https://developers.google.com/workspace/gmail/api/guides/sending
https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
macOS Keychain backend:
https://keyring.readthedocs.io/en/latest/
https://github.com/jaraco/keyring/blob/main/keyring/backends/macOS/__init__.py
Google credential serialization; granted_scopes ayrıca korunur:
https://github.com/googleapis/google-auth-library-python/blob/main/google/oauth2/credentials.py

Bu yardımcı ücretli kaynak veya abonelik açmaz. Mevcut ajans paketi,
arşivler, Pages, başka hesaplar ve sistem Python kurulumu değiştirilmedi.
