HAMLE — YEREL ÇALIŞMA KONTROLLERİ

Bu klasör, devir paketindeki örnek motoru değiştirmeden hazırlanan yerel
kontrollerdir. Ağ, OAuth, mail, Basecamp yazması ve model üretim çağrısı yoktur.
Buluta dağıtılmamıştır. Müşteri akışını etkinleştirme seçeneği içermez.

Paket kökünden tüm yeni testler:
  work/hamle-gmail-connector/.venv/bin/python -B -m unittest discover -s work/hamle-runtime -p 'test_*.py'

Ücretsiz ve sentetik bütünleşik prova:
  work/hamle-gmail-connector/.venv/bin/python -B work/hamle-runtime/dry_run.py

schedule.py: İstanbul hafta içi 08.30–18.30 arasında 21 tur hesabı. Sabah
özeti yalnız başarılı 08.30 turundan sonra bir niyet kaydıdır; gönderilmez.
Tatil politikası bilinmiyor. Gerçek zamanlayıcı ve tur izleme servisi yoktur.

ledger.py: Tek yerel dosyada SQLite ile niyet tekilleştirme, iş/kaynak/sürüm
kapsamı, süreli kilit ve eski çalışanı durduran nesil kontrolü. İnsan devralma
bekleyen niyetleri iptal eder; başlanmış ama sonucu belirsiz işlem tekrar
edilmez. Kabul kaydı yalnız simülasyon kabulüdür, gerçek teslim kanıtı değildir.

budget.py: Google Cloud + OpenAI + görsel üretim için tek 50 USD defter.
Gerçekleşmiş harcama veya faturalama doğrulanmamışsa rezervasyon engellenir.
Belirsiz işlem rezervi tutulur; taşan gerçek maliyet yeni işleri durdurur.
12 Ekim 2026 23.59.59 İstanbul sonrası yeni rezervasyon yapılamaz.
Bu modül sağlayıcı faturası okumaz, sağlayıcı harcamasını teknik olarak kesmez.
Kayıt dışı Cloud giderleri ve dış harcamalar için gerçek mutabakat hâlâ gerekir.

Üretime geçmeden: dağıtık kalıcı depo, güvenilir saat/kimlik, yetkili stop/devir
arayüzü, her dış etkiden önce güncel yetki, gerçek sağlayıcı adaptörleri,
fatura mutabakatı ve uçtan uca kabul gereklidir. SQLite Cloud Run çoklu
örneklerinde ortak üretim veritabanı olarak kullanılamaz.

Gmail normalizer'ın action_eligible=False ve sequence=None çıktısı aynen
korunur. internalDate yetki sırası değildir. Hamle'ye ulaşmayan CC çıkarma
olayını bu kod göremez; E04 açık kalır. Marka ve hizmet onayları tamamlanmış
sayılmaz. 65 iş / 19 tamam / 27 puan başlangıcı değişmez.
