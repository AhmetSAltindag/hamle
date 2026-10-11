Hamle — yerel iç prova kontrol paneli

Bu panel yalnız önceden hazırlanmış, tek iç prova kaydı bulunan SQLite
veritabanını açar. Gmail, Basecamp, Google Cloud veya OpenAI çağrısı yapmaz.
Yerel inceleme durumunu göstermek ve Durdur / Ben devralıyorum seçimlerini
aynı yerel store üzerinden kaydetmek içindir. Yeni kayıt veya yeniden açma
işlemi sunmaz; yürütme yetkisi vermez.

Başlatma (Python 3.12+, standart kütüphane):
  python server.py --db /mutlak/yol/mevcut-inceleme.sqlite3

Ekrana yalnız dinamik localhost URL'si yazılır. Tarayıcıda tam olarak
http://127.0.0.1:PORT/ adresini kullanın. Port seçiminde 0, bind adresinde
yalnız 127.0.0.1 kullanılır; dış arayüzde dinleme seçeneği yoktur.
Ctrl+C sunucuyu kapatır. Sunucu kapanması veritabanını silmez veya inceleme
yetkisi açmaz. Paneli yeniden başlatmak yeni oturum oluşturur.

Güvenlik sınırı:
- Host tam olarak dinamik 127.0.0.1:PORT değeri olmalıdır.
- Mutasyonlar yalnız POST /api/control yolunda; tam Origin, HttpOnly
  SameSite=Strict çerezi ve oturuma bağlı CSRF başlığı gerekir.
- Yalnız {"action":"stop"} ve {"action":"takeover"} kabul edilir.
- İstemciden review_id, veritabanı yolu, dosya adı veya komut alınmaz.
- İstek gövdesi 256 bayt, uygulama başlıkları 8192 bayt ile sınırlıdır.
- Harici kaynak/CDN, CORS, shell, subprocess veya eval yoktur.
- UI/API çıktısı güvenli alanlarla sınırlıdır; mail gövdesi, ekler,
  mail/thread kimliği, fingerprint ve dahili yollar gönderilmez.
- GET /api/control hiçbir mutasyon yapmaz. İstekler veya tokenlar loglanmaz.
- Tarayıcıya yerel erişim kişi kimliği doğrulaması değildir. Bu kontrol
  müşteri operasyonunu veya E04 (ulaşmayan CC çıkarma maili) sorununu çözmez.

Test:
  python -m unittest discover -s . -p 'test_server.py' -v
Testler sahte store ve geçici loopback sunucusu kullanır. Bir entegrasyon
testi gerçek Store ile geçici sentetik SQLite kaydında devralma kalıcılığını
doğrular. Kullanıcının gerçek kaydı veya harici servis kullanılmaz; sadece
standart kütüphane gerekir.
