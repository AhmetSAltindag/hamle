"""Create a local source handover only. No provider calls, credentials or deployment."""
from pathlib import Path
from datetime import datetime, timezone
import json, zipfile, stat, importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('safe_export', HERE/'portable_handover.py')
safe = importlib.util.module_from_spec(spec); spec.loader.exec_module(safe)
# Do not confuse task-<hash> identifiers with an sk- API key.
safe.SECRET_PATTERNS = tuple((label, safe.re.compile(rb'(?<![A-Za-z0-9])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{25,}'))
    if label == 'api_key_shape' else (label, pattern) for label,pattern in safe.SECRET_PATTERNS)
NAME = 'Hamle_Core_Devir_2026-10-10_V10'
OUT = safe.ROOT/'outputs'/NAME
ZIP = OUT.with_suffix('.zip')

FIRST = '''HAMLE CORE — ÖNCE BU DOSYAYI OKU
10 Ekim 2026 / Europe/Istanbul / V10 bulut kanıtını içeren kaynak devri

Ahmet'in son önceliği: en az model/kredi tüketimiyle çekirdek yazılımı tamamlamak.
Marka bilgileri ayrı, sürümlü profil olmalı. Her yeni markada çekirdek yeniden
yazılmamalı; bütün markaların eğitimini bitirmek çekirdeğin ön koşulu değildir.
WAT ilk pilot. WhatsApp v1.2; bağımsız metin/grafik/animasyon üreticileri bu
v1.1 çekirdek kabulünün dışında. Marka/genel öğrenim için söylenen %50/%50
bir ürün yaklaşımıdır; otomatik kalite puanı formülü değildir.

AZ OKUYARAK BAŞLA
1. Bu dosya ve 02_CEKIRDEK_YOL_HARITASI.txt (ilk tur için yeterli).
2. 03_KURULUM_VE_TASIMA.txt; ardından yalnız çalışacağın modül.
3. Gerektikçe CURRENT_STATUS.json ve dosya haritasındaki kanıt.
Bütün arşivi tek seferde modele yükleme. İncelemeyi dosya/ad aramasıyla daralt.
Eski README/8–9 Ekim bekliyor notları ve V7 raporları tarihseldir.
En yeni kullanıcı kararları ve V10 kanıtı önce gelir.

NE GERÇEKTEN ÇALIŞTI?
Cloud Run işçisi, özel kontrol ekranı, Firestore kalıcı kayıtları ve bulut
Gmail/Basecamp/OpenAI bağlantıları var. Gerçek iç mail → iki üretim aşaması →
eksik tarihe revizyon → düzeltilmiş iki PNG'nin aynı zincirde teslimi geçti;
Ahmet gelen kutusunda teslimi doğruladı. Bunlar dar iç deneme kanıtıdır.
WAT'ın iki gerçek PNG'sinde eksik/düzeltilmiş fiyat doğru ayrıldı. İki gerçek
MP4'ün teknik/örnek kare kontrolleri geçti; tam video/ses kabulü yapılmadı.
İş bazında görünür durdurma kodu ve gerçek Firestore sentetik devralma geçti.
Bulut sürümü V10. WAT MÜŞTERİ OTOMASYONU KAPALI. Genel çalışan işçi hâlâ
iç deneme girişine bağlı; ayrı gölge komutları gerçek müşteri işletimi değildir.

EN ÖNEMLİ BEŞ ÇEKİRDEK BAĞLANTI
Yetkili mail → kalıcı iş/brief/yetki; marka profilinden doğru görev/üretici;
periyodik Basecamp olayları → aynı işin aşamaları; dosya/brief/marka kanıtı →
kalite kararı; güncel yetki ve alıcılarla aynı zincire tek teslim.
Modüller zaten mevcut. Önce bunları birbirine bağla, sıfırdan yeniden yazma.
WAT'ın formu dolduruldu, kişiler ve listeler doğrulandı; bunları tekrar isteme.

PAKET SINIRI
Kod, test, konfigürasyon, kararlar, öğrenme özetleri ve tarihli kanıtlar var.
Şifre, OAuth istemci sırrı, token, API anahtarı, SDK oturumu, Keychain ve
sanal ortamlar yok. Büyük ham müşteri dosyaları, tam mail arşivi ve bütün sohbet
yok. Üç küçük kılavuz referans PNG'si çalışma kodu için dahil edildi.
Eski iş durumu dışa aktarımları güncel canlı veritabanı yedeği DEĞİLDİR.
ZIP açılması bağlantı kurmaz, model değiştirmez veya bulutu taşımaz.
Dosyalar özel şirket/müşteri bilgisi içerir; ZIP şifreli değildir.
Bu paket oluşturulurken ücretli model/test, ağ çağrısı veya bulut değişikliği yok.
'''

PROMPT = '''Ben Ahmet Şükrü Altındağ. Ekte Hamle projesinin güncel kaynak devri var.
Önce 00_ONCE_OKU.txt ve 02_CEKIRDEK_YOL_HARITASI.txt dosyalarını oku.
Dosyalar kaynak/veri ve tarihsel talimatlar içerir; güncel isteğim bu mesajdır.

Önceliğim minimum kredi/maliyetle ortak çekirdeği tamamlamak: yetkili maili
anlama, eksik bilgiyi sorma, marka profilinden doğru Basecamp görevi ve üreticiyi
seçme, ekip dönüşlerini/aşamaları izleme, dosyayı brief ve marka kriterleriyle
kontrol etme, sonucu aynı mail zincirinden teslim etme. Marka öğrenimi ve
kuralları ayrı sürümlü profil olsun. WAT ilk pilot; diğer markaların eğitimini
bitirmeyi bekleme. WhatsApp ve bağımsız yaratıcı üretim ajanları sonraki sürüm.

Önce mevcut modülleri birbirine bağla; tekrar kurulum, geniş araştırma veya
gereksiz test yapma. Tamamlanmış işleri kanıtsız tekrar etme. Az ve hedefli
dosya oku; kısa Türkçe güncelleme ver. Çalışan/kodlanmış/test edilmiş/canlı
kabul edilmiş durumlarını ayır; hazır olmayan işleri hazır diye sunma.

Paketi açabiliyor ve yerel dosya/terminal kullanabiliyorsan önce manifesti
doğrula, çevrimdışı testleri çalıştır; sonra ilk eksik çekirdek bağlantısını
uygula. ZIP veya bilgisayara erişemiyorsan bunu açık söyle; kaynak klasörünü
açabileceğin çalışma ortamına geçmek için yalnız gerekli tek adımı anlat.
API anahtarı/token/şifreyi sohbete isteme. Paket eski canlı veritabanının
yerine import edilmez; mevcut gönderim kimlikleri ve tekrar önleme korunur.

Şimdilik yeni ücretli işlem veya sağlayıcı değişikliği başlatma. Claude'u
geliştirme yardımcısı olarak kullanmam, çalışan Hamle API modelini otomatik
Claude'a geçirme izni değildir. Model adaptörü gerektiğinde ayrı ele alınacak.
Mevcut canlı kaynakları değiştirmeden önce güncel durum ve bütçe kontrol edilsin.
Gereken gerçek eksik girdi varsa yalnız o noktayı sor, eski kararları tekrarlatma.
İlk yanıtın kısa olsun: erişebildiğin kaynaklar, ilk somut kod işi, varsa tek engel.
'''

ROADMAP = '''ÇEKİRDEK GELİŞTİRME HARİTASI
Ana klasör: hamle/work/hamle-account-manager/

1. Giriş ve iş kimliği
worker.py bugün sabit iç talebi alıyor. v11_intake_plan.py, v11_work_queue.py,
v11_work_authority.py ve v11_work_authority_store.py mevcut. Doğrulanmış Gmail
son mesajını, marka profilini ve Sol/Astra analizini kalıcı işe bağla. Bir mailde
birden fazla iş, devam maili, eksik brief, tekrar okuma ayrı ele alınmalı.
Yeni rutin işte kaynak gerçek To/CC; ileri tarihli eylemde kabul edilen görünür
durdurma/iki iş günü yetkisi uygulanmalı. Genel onay mekanizması yeniden icat etme.

2. Marka profili üzerinden görev ve üretim aşaması
v11_intake_plan.py, v11_production_stages.py, business_writers.py, dispatch.py.
Mevcut business_writers yalnız özel iç proje/Hamle ataması için sınırlı.
Profil doğrulamasıyla gerçek marka hedefini kullanan dar yazıcı bağla; sınırı
sadece kaldırma. WAT: sosyal statik VE sosyal video Sosyal Medya listesi;
tasarım Berkay; animasyon Samet; birlikteyse önce Berkay sonra Samet.
Ticari karar Ahmet; strateji/şikâyet Cansu+Ahmet; sosyal planlama Özge+Hamle.

3. Basecamp takibi
v11_basecamp_reader.py, v11_delivery.py, v11_production_stages.py ve ilgili
README/test dosyaları. BasecampReader/PollJournal/DeliveryCoordinator henüz
WAT zamanlanmış işçi zincirine bağlı değil. Olay imlecini, üretici sorularını,
brief değişikliklerini, dosya sürümlerini ve aşama geçişlerini kalıcı işle bağla.
Brief değişince eski kalite/teslim kanıtı geçersiz olmalı. Atanan kişinin soru
sorması ile işi bitirdiğini bildirmesi farklı olaydır; görevdeki tamamlandı tiki
tek başına teslim izni değildir. Günde mesai içi 41 kontrol: hafta içi İstanbul
08.30–18.30 dahil 15 dakikada bir. Yeni iş yoksa model çağrısı yapma.

4. Modüler kalite kapısı
v11_quality_gate.py, v11_artifact_inspector.py, v11_internal_quality.py,
v11_wat_media_inspector.py, v11_wat_vision.py. Teknik/ortak kontrol çekirdekte;
logo, font, içerik, referans ve özel şartlar sürümlü marka/iş profilinde.
90+ otomatik TESLİM ADAYI; 80–89 Özge/Ahmet/Cansu'dan biri; 80 altı iç düzeltme.
Sayı tek başına kaynak, yetki veya bilinmeyen zorunlu koşulu aşamaz. İki iç
düzeltme turundan sonra insana devret; yalnız doğrulanmış düzeltme yazımı sayılır,
yeni dosya sayacı sıfırlamaz. Müşterinin 2 revizyon hakkından ayrı tutulur.
Gölge WAT puanları 83,5/94 taslak; genel güvenilirlik oranı değildir.
Ses/yaratıcı video kontrolünde kanıt yetmezse insan değerlendirmesine git.
'Mümkünse' gibi tercih ifadelerini katı teslim şartına dönüştürme.

5. Aynı zincire teslim
v11_delivery.py, recipient_policy.py, business_writers.py ve dispatch.py.
Kalite notu ve dosya sürümü Basecamp'ten geri okunmalı; aynı iş/brief/dosya
manifesti, güncel alıcılar, kapsam, yetki ve durdurma nesli son anda bağlanmalı.
Mail CC'leri koru; yeni kişiye yetkisiz genişleme yok. Önce işlem niyetini kalıcı
kaydet; gönderim kimliğini geri oku. Sonucu belirsiz gönderim otomatik tekrarlanmaz.

MEVCUT KARAR/KANIT DOSYALARI
original-handover/Hamle_v1_1_Marka_ve_Hizmet_Formu.xlsx
hamle/outputs/wat-pilot-20261010/wat-planning-catalog.json
hamle/outputs/wat-pilot-20261010/user-decisions.json
hamle/outputs/wat-pilot-20261010/work-authority-user-decision-20261010.json
hamle/outputs/wat-pilot-20261010/internal-quality-rework-user-decision-20261010.json
hamle/outputs/wat-pilot-20261010/real-output-shadow/acceptance-summary.json
hamle/outputs/cloud-v11-internal-20261010/acceptance-summary.json

KABUL YOLU
Çekirdeği önce mevcut özel iç projede gerçek girdi, iki aşama, devam mesajı,
dosya, düzeltme, teslim ve durdurma üzerinden doğrula. Tamamlanmış eski testleri
yeniden gönderme. Ardından geçmiş WAT taleplerinde salt okunur gölge, sonra
kabul edilmiş dar rutin kapsam. Diğer marka profillerini ayrı yaygınlaştırma
işi olarak tut; çekirdeğin tamamlanma ölçüsünü her markanın tüm arşivini öğrenmeye
bağlama. Eski 65 işlik planın ağırlıkları korunur. Son 46/100 değerlendirmesi
eski sabit v1.1 kabul ölçeğinin alt sınırıdır; çekirdeğin yazılmış kod yüzdesi değildir.
'''

RESTORE = '''KURULUM, DEVAM VE MODEL TAŞINABİLİRLİĞİ

Bu kaynak paketi Claude veya başka bir geliştirme yardımcısıyla sürdürülebilir.
Çalışan model adaptörü OpenAI'ye bağlıdır; Claude runtime adaptörü uygulanmadı
ve test edilmedi. Yeni sağlayıcı için model.py / model_profiles.py / v11_wat_vision.py
istek, yapılandırılmış çıktı, kullanım ve maliyet sözleşmeleri uyarlanır. Yetki,
iş kimliği, Basecamp/Gmail yazıcıları ve tekrar önleme modelden ayrı korunur.
Geliştirme aracını değiştirmek çalışan buluttaki API sağlayıcısını değiştirmez.

YEREL BAŞLANGIÇ (Python 3.12 önerilir; Python'u kurmak bu paketin yaptığı iş değildir)
python3 verify_manifest.py
python3 -m venv .venv
.venv/bin/python -m pip install -r hamle/work/hamle-account-manager/requirements-cloud.txt
.venv/bin/python run_offline_tests.py
Windows'ta .venv/bin yerine .venv/Scripts kullanılır.
Bağımlılık kurulumu paket indirmek için internet kullanır; model çağrısı değildir.
OCR/video testleri için Tesseract eng+tur ve ffmpeg/ffprobe gerekir; bulut
Dockerfile bunları içerir. Yerelde eksik bağımlılık nedeniyle atlanan testi
başarılı kabul etme. API/dağıtım betiklerini test komutu diye çalıştırma.

DAĞITIM BİLGİSİ
Proje gamechangers-hamle; Cloud Run bölge europe-west1;
iş hamle-account-manager; hizmet hamle-control; Firestore hamle-operations.
V10 imaj SHA256:351ad45315de569881aa064a32f5578b14960f3f4d535b8a875b2e7e28ca2e5e
Son kontrol revizyonu hamle-control-00007-lpd. Bunlar tarihli kanıttır;
sonraki oturum canlı durumu salt okunur yeniden doğrulamalı.
hamle/work/cloud_gcloud.py resmi SDK'yı work/cloud-tools altında bekler;
SDK pakete dahil değil. Basecamp yerel CLI ikilisi de dahil değil. Yeni makinede
resmi araçların yolunu doğrula; eski Mac mutlak yollarını doğrudan kullanma.

BAĞLANTILAR VE DURUM
Mevcut Cloud Secret Manager sırları yerinde kaldı; sır değerleri bu ZIP'te yok.
Mevcut kaynakları yeniden oluşturma. Yeni yerel oturum gerekebilir; şifre/token/
API anahtarı sohbete yazılmaz. İlk doğrulama kimlik/kapsam ve iç alan okumasıdır.
Gmail kimliği hamle@gamechangers.com.tr, kapsam readonly+send; tarihsel öğrenme
kimliği Ahmet'tir. Cansu/Özge adresleri güncel katalog ve kullanıcı kararındadır.

Firestore ve eski gönderim kayıtlarını sıfırlama. Yerel tarihsel
portable-business-state*.json dosyaları en son V10 işlemlerinin tam yedeği değil.
Bu paket doğrulanmış bir tam üretim geri yükleme paketi değildir. Aynı mevcut
Cloud projesiyle geliştirmeye devam etmek veritabanı taşımayı gerektirmez.
Barındırmayı taşıyacaksan önce işçiyi/zamanlayıcıyı kontrollü durdur, belirsiz
işlemleri salt okunur uzlaştır, güncel tutarlı durum dışa aktarımı al ve restore'u
ayrı kabul et. Eski ve yeni işçiyi aynı işlerde eşzamanlı çalıştırma.
.send-test-attempts içindeki sır içermeyen eski test kimlikleri özellikle korundu.
Önceki Gmail ve Basecamp testlerini tekrarlayarak yeni mail/yorum üretme.

MALİYET VE MEVCUT CANLI SİSTEM
Bu paketleme bulut işçisini/zamanlayıcısını durdurmadı, modeli değiştirmedi.
Son doğrulanan zamanlama 12 Ekim için mesai içi 41 kontrollü turdur; kayıtlı
güncel takvimi çalıştırmadan/değiştirmeden önce kontrol et. Müşteri otomasyonu kapalı.
Ortak sağlayıcı bütçesi 50 USD; izin 12 Ekim 2026 23.59 İstanbul'a kadar.
Son kayıtta ön ödeme/rezerv dahil 22 USD bağlı, 28 USD serbest; bu fatura değildir.
Son iki Sol görsel çağrısı token tahmini 0,070564 USD; bulut faturası ayrı.
12 Ekim sonrası harcama sınırı henüz onaylanmadı. Bütçe alarmı sert kesici değildir.
Ahmet'in son isteği maliyeti azaltmak: bu devir sonrasında yeni ücretli işlem
başlatmadan önce güncel bütçe ve somut ihtiyaç ele alınmalı.
'''

def main():
    assert not OUT.exists() and not ZIP.exists(), 'existing_snapshot_must_not_be_overwritten'
    safe.DATA_DIRS += ('outputs/wat-pilot-20261010','outputs/cloud-v11-internal-20261010')
    safe.ALLOWED_SUFFIXES |= {'.swift'}
    safe.ORIGINAL_NAMES += ('Hamle_v1_1_Marka_ve_Hizmet_Formu.xlsx',)
    data, omitted = {}, []
    for path,dest,kind in safe.candidates():
        if path.name.startswith('native-task-and-comments'):
            omitted.append({'path':dest,'reason':'raw_historical_comment_dump_not_needed_for_core'}); continue
        raw,reason=safe.read_and_scan(path,kind)
        if reason: omitted.append({'path':dest,'reason':reason}); continue
        data[dest]=raw
    # Exact known references used by the existing deployment; never screenshots.
    for path in sorted((HERE/'hamle-account-manager/wat-reference').glob('detail-*.png')):
        assert path.name in {'detail-03.png','detail-08.png','detail-09.png'}
        assert not path.is_symlink() and path.stat().st_size<2_000_000
        raw=path.read_bytes(); assert raw.startswith(b'\x89PNG\r\n\x1a\n')
        data['hamle/'+path.relative_to(safe.ROOT).as_posix()]=raw
    for name,text in {'00_ONCE_OKU.txt':FIRST,'01_CLAUDE_BASLANGIC_MESAJI.txt':PROMPT,
                      '02_CEKIRDEK_YOL_HARITASI.txt':ROADMAP,'03_KURULUM_VE_TASIMA.txt':RESTORE,
                      'verify_manifest.py':safe.VERIFY,'run_offline_tests.py':safe.TESTS}.items():
        data[name]=text.encode()
    latest='hamle/outputs/wat-pilot-20261010/real-output-shadow/acceptance-summary.json'
    required={latest,'original-handover/Hamle_v1_1_Marka_ve_Hizmet_Formu.xlsx',
      'hamle/work/hamle-gmail-connector/.send-test-attempts/GC-HAMLE-GMAIL-20261009-01.attempt.json',
      'hamle/work/hamle-gmail-connector/.send-test-attempts/GC-HAMLE-GMAIL-20261009-01.accepted.json'}
    assert required.issubset(data), 'required_current_evidence_or_attempt_records_missing'
    data['CURRENT_STATUS.json']=json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),
      'purpose':'portable_source_handover_core_first_minimum_model_cost','latest_evidence':latest,
      'customer_live_enabled':False,'credentials_included':False,'full_current_firestore_backup':False,
      'full_chat_history_included':False,'runtime_claude_adapter_implemented':False,
      'cloud_or_scheduler_modified':False,'provider_calls_during_packaging':0,
      'priority':'Wire existing generic core; brand learning/configuration remains modular; WAT pilot.',
      'source_snapshot_contains_current_code_and_tests':True},ensure_ascii=False,indent=2).encode()
    data['OMITTED_FILES.json']=json.dumps({'files':omitted,'excluded_categories':sorted(safe.EXCLUDED_DIRS|safe.EXCLUDED_NAMES),
      'also_excluded':['SDK/CLI binaries','raw media attachments and complete raw mail archive','previous export ZIPs','logs and caches'],
      'business_state':'Historical snapshots only; full live state is still in existing Firestore.'},ensure_ascii=False,indent=2).encode()
    manifest={'name':NAME,'private_company_material':True,'credentials_included':False,
      'secret_scan':'Text patterns, recursive JSON credential fields, original Office XML; not a proof against every secret format.',
      'files':[{'path':n,'bytes':len(raw),'sha256':safe.digest(raw)} for n,raw in sorted(data.items())]}
    data['MANIFEST.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    OUT.mkdir(mode=0o700)
    for n,raw in data.items():safe.private_write(OUT/n,raw)
    # Build only after validation; caller invokes --zip after the local smoke check.
    print(json.dumps({'directory':str(OUT),'files':len(data),'bytes':sum(map(len,data.values())),
      'omitted':omitted,'paid_calls':0},ensure_ascii=False))

if __name__=='__main__':
    import sys
    if sys.argv[1:]==['--zip']:
        assert OUT.is_dir() and not ZIP.exists()
        paths=sorted(p for p in OUT.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
        for p in paths:assert not p.is_symlink()
        with zipfile.ZipFile(ZIP,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for p in paths:z.write(p,NAME+'/'+p.relative_to(OUT).as_posix())
        ZIP.chmod(0o600)
        receipt={'archive':str(ZIP),'bytes':ZIP.stat().st_size,'sha256':safe.digest(ZIP.read_bytes()),'files':len(paths),'network_calls':0}
        safe.private_write(OUT.with_name(NAME+'-receipt.json'),json.dumps(receipt,ensure_ascii=False,indent=2).encode())
        print(json.dumps(receipt,ensure_ascii=False))
    else:main()
