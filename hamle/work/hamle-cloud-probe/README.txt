HAMLE — YALNIZ SENTETİK CLOUD RUN JOB İSKELETİ

Bu klasör 10 Ekim 2026'da sentetik prova için dağıtıldı. Gerçek Cloud Run
ve Firestore üzerinde kalıcı kayıt, tekilleştirme, eşzamanlı tek sahiplik ve
onaylanmış durdurma kaydından sonra iş açmama doğrulandı. Müşteri akışı yoktur.
Gmail, Basecamp, OpenAI, mail gönderimi veya model çağrısı içermez.
65 iş / 19 kabul / 27 puan / 0 canlı ajan durumu bu testlerle değişmez.

Yerel ve ağsız doğrulama (bu klasörde):
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest discover -p 'test_*.py'
Eksik ortamla python main.py run çalıştırmak ağdan önce reddedilir.

Gelecekte yapılacak açıkça izinli Firestore Native provası için zorunlu ortam:
  HAMLE_PROJECT_ID=gamechangers-hamle
  HAMLE_PROBE_NAMESPACE=hamle-synthetic-probe-v1
  HAMLE_FIRESTORE_DATABASE=(default) veya hamle-synthetic
  HAMLE_PROBE_RUN_ID=synthetic-20261010-01
  HAMLE_ALLOW_FIRESTORE_PROBE=yes
Varsayılan proje/veritabanı/namespace/çalışma kimliği seçilmez. Run ID yalnız
sentetik test kimliği içermeli; kişi, marka, mail veya müşteri verisi koyulmaz.
Bulut kimliği platformun bağlı hizmet hesabından gelir; image'a anahtar girmez.
Yerel ADC veya sır dosyaları bu klasöre kopyalanmaz.

Komutlar: initialize / arm / run / stop.
initialize kontrol belgesini DURDURULMUŞ oluşturur; var olanı sıfırlamaz.
arm ve stop kontrol neslini artırır. run yalnız açık kontrolle başlar.
Kontrol ve run aynı transaction içinde okunur. Tamamlama da kontrolü yeniden
okur; arada stop veya tekrar arm olduysa eski çalışan tamamlanamaz.
Tek çıktı sabit 5050 sayısıdır. Harici eylem yoktur. Kontrolün ardından harici
API çağırmak için bu örnek kopyalanamaz; aradaki yetki yarışını çözmez.

Aynı run ID yeniden çağrılınca tamamlanmış kayıt tekilleştirilir. Başlanmış,
kesilmiş veya sonucu belirsiz kayıt asla otomatik yeniden alınmaz. Çökme iki
transaction arasındaysa kayıt started olarak kalır, inceleme gerektirir.
SDK/commit hatasında yalnız unknown_hold çıkar, ham hata/sır yazdırılmaz.
Transaction uygulama denemesi 1; Cloud Run Job da max-retries=0 ve tasks=1
olarak ayrıca kurulmalıdır. Bu dosya dağıtım ayarlarını kurmaz.
Çıkış kodları: 0 beklenen sentetik sonuç, 2 önkoşul reddi, 3 tutulan/kesilmiş
iş, 4 belirsiz hata. 0 tek başına kabul değildir; outcome okunmalıdır.

Docker build context yalnız BU klasör olmalı. .dockerignore yalnız beş
gerekli dosyaya izin verir. Gerçek Cloud Build başarılı; dağıtım image digest
ile sabitlendi. Alt bağımlılıkların tam lock/hash denetimi yapılmadı.
Üst paket 2.34.0 sabitlendi.
Resmî sürüm kaynağı:
https://docs.cloud.google.com/python/docs/reference/firestore/latest/changelog
Transaction API kaynağı:
https://docs.cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1.transaction.Transaction

BU İSKELET HARCAMA SINIRI DEĞİLDİR. Firestore/Cloud Run/registry/build gideri
oluşabilir. Gerçek harcama, kalan ortak 50 USD bütçe ve fatura önce doğrulanmalı.
İşçi her yeni Firestore transaction öncesinde güncel saati yeniden kontrol eder;
12 Ekim 2026 23.59.59 İstanbul sonrasında yeni transaction başlatmaz. Başlamış
bir ağ çağrısı/transaction süreyi aşabilir; bu kod onu veya çalışan kaynakların
faturasını kesmez. stop komutu da bu süre sınırına tabidir: süre dolmadan stop
uygulanmalı; süre sonrası gerekli kapatma/temizlik ayrıca kullanıcı onayıyla
yapılmalıdır. Zamanlayıcı kurulu değildir.
Gerçek Cloud kanıtı için IAM, API, bölge/veritabanı, hizmet hesabı, maliyet,
kalıcı kayıt, iki çağrıda tekilleştirme, stop ve gerçek yarış/çökme denemeleri
ayrıca doğrulanmalıdır. Yerel fake testleri bu kanıtların yerine geçmez.
E04 (Hamle'nin görmediği CC çıkarma) ve üretim yetki/devralma hâlâ açıktır.

10 Ekim gerçek prova kanıtı: ../../outputs/new-mac-2026-10-10/cloud-foundation.json
Toplam 9 kısa execution, 1 build; kontrol son durumda kapalı, zamanlayıcı yok.
İlk son test, kuyrukta bekleyen stop tamamlanmadan başlatıldığı için stop'tan
önce çalıştı; bu sonuç durdurma kabulü sayılmadı. Kapalı durum API'den
doğrulandıktan sonraki ek test yeni kayıt açmadı. Bu sıralama hatası kanıtta
korundu. Cloud Run Job başlatmak anlık durdurma değildir; üretimde görünür
insan devralma yolu ve durdurma onayının kalıcı kaydı ayrıca kurulmalıdır.
3 USD rezerv gerçek harcama sayılmaz; fatura mutabakatına kadar tutulmaktadır.
