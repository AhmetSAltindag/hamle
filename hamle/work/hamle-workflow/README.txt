HAMLE GMAIL BAŞLIK VE MIME NORMALLEŞTİRİCİSİ

Durum: E03/E04 için yerel yazılım hazırlığı; canlı kabul veya çalışan otomasyon
değildir. Bu hazırlık sırasında gerçek API, OAuth, Keychain, tarayıcı, dosya
indirme, mail gönderme ve Basecamp işlemi yapılmadı. mail_ingest.py yalnız
Python standart kütüphanesiyle verilen sözlüğü işler; kaynak veriyi değiştirmez.
İsteğe bağlı gerçek okuma bağlayıcısının ayrı sözleşmesi aşağıdadır.

Sözleşme
normalize_thread(response, context=Context(...)) zaten alınmış Gmail
threads.get format=full biçimindeki sözlüğü kabul eder. Mevcut Gmail
yardımcısının read-test'i yalnız id,messages/id ister; bu veri yeterli değildir
ve reddedilir. Bu modül yardımcıya yeni ağ/okuma yetkisi eklemez.

Context içindeki doğrulanmış sağlayıcı yanıtı, Hamle hesap kimliği ve tam
görünür anlık görüntü bilgisi gelecekte güvenilir bağlayıcıdan gelmelidir;
JSON içindeki benzer alanlar yetki vermez. Parser sağlayıcı özgünlüğünü
kanıtlayamaz. Yetkili gövde okuma kimlikleri ayrıca mesaj bazında verilir.
Varsayılan bağlam güvenilmezdir ve gövdeler açılmaz. Ahmet tarihsel öğrenmesi
bu modülün canlı Hamle yetki yoluna bağlanmaz; ayrı kaynak/amaçtır.

Yalnız üst payload.headers içindeki gerçek To/Cc ayrıştırılır. Tam adres
hamle@gamechangers.com.tr olmalıdır; büyük/küçük harf normalize edilir.
Görünen kişi adı, benzer/plus adres, grup takma adı, BCC, teslim kopyası,
Reply-To, alıntı/gövde ve eklenmiş mail başlığı katılım vermez. Birden çok
To/Cc/From başlığı, bozuk adres veya başlık belirsizliktir. Own From/SENT/
DRAFT ve bildirilen otomatik yanıt/listeler iş girdisi olmaz. Otomatik yanıt
işaretlerinin yokluğu bir insanın yazdığını semantik olarak ispatlamaz.

Kimlik ve sıra
Gmail message/thread id'leri hex biçiminde doğrulanır; RFC Message-ID kimlik
yerine geçmez ve çıktı kimliğine taşınmaz. Yanlış zincir veya yinelenen id
reddedilir. internalDate yalnız görünen gelen iletilerin gözlemsel sırasıdır.
Eksik/eşit tarih, eksik görünür snapshot veya belirsiz başlıkta sıra kapalıdır.
Date, historyId ve dizi sırası yetki sequence'i yapılmaz. Son görünen gelen
ileti hariçse eski dahil iletinin gövdesi açılmaz; yeni dahil iletinin gövdesi
ancak bağımsız okuma izniyle açılabilir. Bu, eski kuyruğu yeniden başlatmaz.

Her sonuçta action_eligible=False ve engine_evidence.sequence=None'dır.
Mevcut offline Engine bu kanıtı doğru olarak reddeder. Üretimde sağlayıcı
sırası/katılım geri çekme, iş/marka yetkisi, insan devri, kilit ve her aksiyon
öncesi yeniden kontrol ayrı uygulanmadan bu alanlar true/tamsayı yapılmamalı.
Bu modülün gözlemsel maksimumunu doğrudan engine sequence'e çevirmeyin.
Hamle'ye ulaşmayan CC çıkarılmasını bilemez. Açık yetkili durdurma/devir
olayı veya ayrıca doğrulanmış görünürlük çözümü gerekir; bulunmayan maili
tespit ettiği iddiası yoktur. Canlı tam-dizi görünürlüğünü kanıtlamaz.

Gövde ve MIME
Tüm görünür iletilerin sınırlı zarf metadata'sı ve MIME/ek envanteri çıkar.
Gövde ancak güvenilir canlı bağlam + gerçek To/CC + bağımsız mesaj okuma izni
+ benzersiz son görünür gelen ileti koşullarıyla base64url/charset çözülür.
HTML yalnız güvensiz ham metindir; render, link açma veya talimat yürütme yok.
Ekler/ekli mailler açılmaz. attachmentId envanterdir; dosya adı güvensiz
metindir, yol olarak kullanılmaz. content_inspected=False ek incelemesinin
yapılmadığını söyler. Harici gövde parçası indirilmez. Gövde için yalnız
text/plain ve text/html; kapsayıcı olarak multipart/mixed, alternative ve
related desteklenir. Ek olmayan desteklenmeyen bir parça (RTF dahil), eksik
alt parça veya MIME türü/başlığı çelişkisi varsa tüm gövde withheld kalır ve
somut issue kodu verilir. Alternative içinden seçici parça atlanmaz. Açıkça
ek olan dosya bu gövde kuralından ayrıdır; yine açılmadan envanterde kalır.
Bozuk/eksik parçada kısmi gövde tam okunmuş gibi sunulmaz.
Mesaj/MIME derinlik/parça/gövde boyutu
sınırları vardır. Büyük veya desteklenmeyen kayıt durur; gizli atlama yoktur.
Metadata ve çözülmüş metin kişisel/iş verisi içerebilir; günlüklerde toptan
yazdırılmamalıdır. Normalizasyon marka, müşteri yetkisi, prompt injection
dayanıklılığı veya içerik doğruluğu kontrolü değildir.

Test
  python3 -B -m unittest discover -s work/hamle-workflow -p 'test_mail_ingest.py' -v
Testler sentetiktir; ağ soketi oluşturma kapalıdır. Gerçek hesap verisi ve
kimlik bilgisi kullanılmaz. Sonuç gerçek Gmail/API/teslim veya E03/E04 canlı
kabulü sayılmaz.

TEK ZİNCİR TAM OKUMA BAĞLAYICISI — gmail_full_reader.py

Dar API: read_full_thread(thread_id, *, body_message_id=None).
İçe aktarmak hesap erişimi başlatmaz. Yalnız çağrıldığında mevcut Gmail
yardımcısının MacKeychainStore ve verified_gmail yolunu kullanır: kayıtlı tam
gmail.readonly + gmail.send kapsamını ve taze getProfile sonucunun Hamle
olduğunu doğrular. Yenileme gerekiyorsa mevcut helper bunu yapıp yalnız
Keychain'e kaydeder; hata halinde mail okuması başlamaz. Bu bağlayıcı send
kapsamını kullanmaz; yeni OAuth/onay, başka hesap seçimi veya dosya fallback'i
yoktur. Mevcut helper'ın üretim fonksiyonları değiştirilmedi.

Çağıranın verdiği tek thread kimliği hex doğrulamasıyla küçük harfe çevrilir.
Arama, zincir keşfi, polling, gönderim, ek indirme ve Basecamp yüzeyi yoktur;
komut satırı arayüzü de yoktur. threads.get(format=full) yalnız
id,messages(id,threadId,labelIds,internalDate,payload) alanlarını ister.
Yanlış zincir, eksik/bozuk yanıt veya beklenmeyen sayfalama işareti reddedilir.

Bu istek sağlayıcıdan tüm görünür zincirin gövde verisini belleğe alabilir;
varsayılanın gövdesiz olması ağdan yalnız metadata alındığı anlamına gelmez.
Varsayılan çıktı hiçbir çözülmüş gövde veya ham başlık içermez. Tam sözlük
kaydedilmez ve döndürülmez. Gövde izni isteğe bağlı tek body_message_id ile
verilir; liste, başka zincirin mesajı veya geçersiz kimlik kabul edilmez.
Yalnız bu kimlik normalizer'a izin olarak geçirilir. Normalizer ayrıca
doğrudan To/CC, kendi/otomatik mail ve son görünür ileti koşullarını uygular;
istenen eski gövdenin yerine yeni gövde verilmez. Diğer gövdeler çıkışta da
maskelenir. Ham HTML yine render edilmez.

complete_visible_snapshot yalnız çağrılan tek, filtrelenmemiş provider yanıtı
anlamındadır; görünmeyen mesajı, eşzamanlı değişiklikleri veya küresel son sıra
yetkisini kanıtlamaz. action_eligible=False ve sequence=None korunur. Bu
bağlayıcı E03 hazırlığıdır; E03/E04 canlı kabulü veya müşteri özerkliği değildir.

Çıktı sözlüğünün observation alanı normalizer sonucudur. Oradaki external_calls=0
yalnız saf normalizer'a aittir; bağlayıcı profil + tam-zincir API okuması yapar
ve bunları gmail_read_operations alanında belirtir. Gerekli token yenilemesi
ayrıca token_refreshed alanındadır. mailbox_writes=0 mail kutusu değişikliği
olmadığını söyler; token yenilendiyse Keychain güncellemesini dışlamaz.

Hatalar yalnız sabit ReadError kodlarıdır; ham provider exception metni veya
traceback zinciri gösterilmez. Bağlayıcı stdout/stderr yazmaz; çağrı süresince
kütüphane log ve çıktısını atar, sonra önceki ayarları geri yükler. Bu geçici
ayarlar süreç geneline etki ettiği için tek iş yapan, ayrı manuel süreçte
kullanılmalıdır; çok iş parçacıklı üretim worker'ı için uygun olduğu iddia
edilmez. Dönen normalize metadata da kişi/iş verisi içerebilir; topluca
yazdırılmamalıdır. Çağıran yalnız sabit hata kodunu raporlamalı, exception
nesnesini/debug yerellerini kaydetmemelidir.

Taşınabilirlik: dosya yolları çalışma dizininden değil __file__ konumundan
türetilir; hamle-workflow ve hamle-gmail-connector kardeş klasör olarak
korunmalıdır. Gerçek kullanım mevcut helper'ın bağımlılık ortamını ve macOS
Keychain'i gerektirir. Başka işletim sisteminde dosyaya token düşürmek yerine
durur. Bulut kimlik/sır deposu uyarlaması henüz yapılmamıştır.

Tüm yerel testler:
  python3 -B -m unittest discover -s work/hamle-workflow -p 'test_*.py' -v
Bağlayıcı testleri gerçek helper kodunu sahte kayıt, kimlik bilgisi ve servisle
çalıştırır; gerçek Keychain veya ağ kullanmaz. Gerçek thread gözlemi ayrıca
yetkili çağıranın yapacağı kabul adımıdır; bu hazırlıkta çalıştırılmadı.
