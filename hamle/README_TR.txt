HAMLE — YENİ MAC TEKNİK DEVAM PAKETİ / 10 EKİM 2026

Bu paket kodu, kuralları ve özetlenmiş öğrenimi taşır. Çalışan bulut servisi,
kurulu uygulama veya yeni Mac'e aktarılmış canlı bağlantı değildir.

ÖNCE OKUYUN
1. handoff/GUNCEL_DURUM_2026-10-10.json: güncel durum ve kaynak önceliği.
2. work/project-tracker/project-plan.json: görevler, bağımlılıklar, kabul ölçütleri.
3. work/hamle-workflow/README.txt: okuma ve To/CC güvenlik sınırları.
4. MANIFEST.json: kaynak allowlist'i, dosya hashleri ve açık temizleme kaydı.

KLASÖR YAPISI VE ESKİ YOLLAR
ZIP içeriğini tek proje klasörüne çıkarın; work/ ve outputs/ aynı kökte kalsın.
Örnek <YENI_KOK> istediğiniz yeni çalışma klasörüdür. Dosyaları birbirinden
ayırmayın: bazı testler outputs/gamechangers-pilot/offline yolunu, Gmail
okuyucu ise kardeş work/hamle-gmail-connector klasörünü bekler.
JSON içindeki eski çalışma klasörü önekleri kaldırıldı. Bunlar paket köküne
göre yollardır; XDG ortam değişkenleri gibi mutlak yol gereken yerlerde
<YENI_KOK> ile birleştirin. Eski Mac'in kullanıcı yolunu kopyalamayın.

YENİ MAC'TE YEREL HAZIRLIK (HENÜZ ÇALIŞTIRILMADI)
Python 3.10+ gerekir; önce python3 --version ile mevcut sürümü doğrulayın.
Paket kökünde:
  python3 -m venv work/hamle-gmail-connector/.venv
  work/hamle-gmail-connector/.venv/bin/python -m pip install --index-url https://pypi.org/simple --no-cache-dir --only-binary=:all: -r work/hamle-gmail-connector/requirements.txt
requirements.txt aynı klasördeki constraints.txt ile sürümleri sabitler.
Mevcut Mac'in .venv klasörü taşınmadı. Bu kurulum ağdan paket indirir;
paketi açmak veya dosyaları okumak kendiliğinden kurulum yapmaz.
Ardından yalnız yerel/sentetik testler, paket kökünden:
  work/hamle-gmail-connector/.venv/bin/python -B -m unittest discover -s work/hamle-gmail-connector -p 'test_*.py'
  work/hamle-gmail-connector/.venv/bin/python -B -m unittest discover -s work/hamle-workflow -p 'test_*.py'
  work/hamle-gmail-connector/.venv/bin/python -B outputs/gamechangers-pilot/offline/run_tests.py
Bunlar canlı model veya müşteri işi kabulü değildir. SQLite demo veritabanı
ve eski çalışma günlükleri taşınmadı; testlerin ürettiği kayıtlar sentetiktir.
build_brand_readiness.mjs yardımcı betiği için ayrıca Node gerekir;
JSON'u okuyarak devam etmek için Node kurulması zorunlu değildir.
Belge veri kaynağı ve son Word/Excel üretim betikleri de taşındı. Word
betiği python-docx, Excel betiği @oai/artifact-tool ve Node, Excel kontrolü
openpyxl ister. Eski uygulamanın paketlenmiş çalışma ortamları taşınmadı;
yeni Codex'te çalışma ortamı bağımlılıklarını araçla bulun. Bu kütüphaneyi
rastgele bir npm paket adıyla değiştirmeyin. Hazır Word/Excel ayrı teslimdir.

GMAIL BAĞLANTISI
Google Cloud projesi şirkete aittir; mailbox OAuth kimliği Hamle'dir.
OAuth client JSON'u, refresh/access token ve macOS Keychain taşınmadı.
Yeni Mac'te kullanıcı onayıyla mevcut şirket projesinin Desktop OAuth
istemcisini güvenli özel konumdan kullanın; içeriği sohbete/Git'e koymayın.
Yeni istemci/proje açılmasını varsaymayın. Ayrıntılı auth ve durum komutları
work/hamle-gmail-connector/README.txt içinde; bu README'deki ilk geliştirme
durumu tarihsel bağlamdır, güncel canlı test kanıtları outputs/ içindedir.
Yeni Mac'te kendi Keychain kaydı oluşturulup hamle@gamechangers.com.tr ve
tam gmail.readonly + gmail.send kapsamı doğrulanmalı. Token dosyasıyla geri
dönüş yoktur. İlk canlı kontrol yalnız profil ve bilinen teknik zincirin
minimal okumasıdır; gönderim değildir. Aktarılmış API yetkisi varsaymayın.

TEKRAR GÖNDERİMİ ÖNLEME — BU KAYITLARI KORUYUN
work/hamle-gmail-connector/.send-test-attempts içindeki iki JSON dosyası
önceki iç testin claim/accepted kayıtlarıdır; sır içermez, aynen taşındı.
Bunları silmeyin. GC-HAMLE-GMAIL-20261009-01 testini yeniden göndermeyin;
yeni test ID vererek aynı testi de tekrarlamayın. Önce yalnız read-test
yapın. Gelecekte yeni bir test gerekirse somut yeni amaç ve yetkiyle açılır.
Mac değişikliği gönderim izni veya yeni deneme hakkı oluşturmaz.

BASECAMP BAĞLANTISI
Mach-O binary pakete konmadı. handoff/Basecamp_Kurulum_Notu.json önceki
resmî 0.13.0 sürümünü, resmi release URL'sini ve doğrulanmış arm64 hashlerini
taşır. Yeni Mac'in mimarisine uygun resmi dosyayı seçip kendi checksum'uyla
doğrulayın; Intel dosyayı arm64 hash'iyle karşılaştırmayın. Kurulu CLI'yi
work/basecamp-tools/bin/basecamp yoluna koyun veya çağıranın yolunu güncelleyin.
XDG_CONFIG_HOME=<YENI_KOK>/work/private-connections/config
XDG_CACHE_HOME=<YENI_KOK>/work/private-connections/cache
BASECAMP_PROFILE=hamle-gamechangers
Bu klasörlerin içeriği ve OAuth oturumu taşınmadı; yeni Mac'te yeniden izin
verilir. Kimlik: Hamle AI/53358100; hesap5898865; iç proje49184053.
Kimlik + keyring saklama durumu taze doğrulanmadan yazma yok. CLI'nin dosyaya
düz metin credential fallback'i kabul edilmez. Önce yalnız people show me,
projects show49184053 ve mevcut test görevi okuması yapılır. B09 zaten geçti;
eski/yeni görev açarak tekrarlanmaz. Auth yenileme ve iptal ayrı testlerdir.

ÖĞRENİM VE İŞ YETKİSİ
İlk tur20 kaynak kapsamlı gözlem ve ikinci tur60 ayrı ayrıntılı konu korunur:
WAT27, Rabam26; eski birleşik ya da/Multicosmetics/Remedium grubunda7.
Bu7 konunun güncel marka ayrımına kaynak bazında eşlenmesi bekler. Güncel
8 marka: WAT Mobilite, Rabam, ya da, Multicosmetics, Altınmarka, Bikaldi,
HAN Spaces ve iç marka Gamechangers. ya da ile Multicosmetics birleşmez.
Kaynak mesaj/task kimlikleri ve tarih sınırlamaları
korunur; ham e-postalar, ekler ve tam arşiv yoktur. Tüm arşivin öğrenildiği
iddiası yoktur. Eski iş onayı/görev durumu güncel onay değildir. Normalize
raporlardaki kişi ve iş bilgileri hâlâ şirket verisidir; paket özel tutulur.
20 kurgusal Hamle provası örnektir; gerçek gelen/giden mail değildir.
Markalar ayrı kabulden geçer; dört rolün tamamı henüz canlı değildir.

BÜTÇE VE BULUT
9 Ekim onayı:12 Ekim2026 23:59:59 İstanbul'a kadar sağlayıcılar ortak50USD;
otomatik yenilenmez, yeni Mac'te sıfırlanmaz. Harcanmış tutar/geriye kalan
bütçe bilinmeden ücretli işe başlanmaz. Eski8 Ekim test onayı genişletilmez.
Kullanıcı API için gpt-6-astra/max seçimini onayladı; gerçek model API erişimi
henüz doğrulanmış değildir. Bulut runtime/deploy, kalıcı üretim DB/kuyruk,
müşteri aksiyonu ve dört rol canlı kabulü bu paketi açmakla kurulmuş olmaz.
Hamle'ye ulaşmayan CC çıkarma olayının algılanamaması E04'te açık kalır.
Planlanan düzen hafta içi İstanbul08:30–18:30 dahil yarım saatte bir21 tur;
sabah özeti ilk başarılı08:30 turundan sonra Hamle'den Ahmet'e gönderilir.
Zamanlayıcı henüz kurulmadı.50USD harcama yetkisidir; gerçek kredi/kota,
faturalama ve teknik durdurma mekanizması henüz doğrulanmış değildir.

DAHİL OLMAYANLAR
OAuth client JSON/token, Keychain, .env, private-connections içerikleri,
.venv/node_modules, Basecamp binary, Git, ham mail arşivi, ekran görüntüleri,
ham günlükler, SQLite/WAL/SHM, ciphertext ve geçici/imzalı dosya URL'leri.
Word/Excel takip belgeleri ayrıca teslim edilir; ZIP bunları içermez.
Kaynak dosyalar değiştirilmedi. Yalnız paketteki kopyalar temizlendi;
MANIFEST.json her dönüşümün dosyasını/JSON alanını açıkça kaydeder.
