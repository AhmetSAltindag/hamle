Hamle v1.1 — Puanlı kalite ve teslim kararı
=========================================
Durum: Yerel, canlıya açılmamış geliştirme. Gerçek görsel/video incelenmedi;
model, Gmail, Basecamp veya bulut çağrısı yapılmadı. Saf modül ve teslim
koordinatörü bağlantısı sentetik kanıtla sınandı. Yerel test başarısı WAT
müşteri operasyonunun kabul edildiği anlamına gelmez.

Kural
- 90 ve üstü: delivery_candidate. Hamle'nin otomatik teslim yolu için kalite
  koşulu sağlanır; gönderim yetkisi, güncel kaynak, güvenlik ve kalıcı kayıt
  ayrıca doğrulanır. Bu sonuç tek başına sağlayıcı çağrısı oluşturmaz.
- 80 dahil, 90 hariç: needs_human_approval. Doğrulanmış yetkili kişinin bu
  çıktıya ve kalite raporuna bağlı süreli onayıyla aday aşamasına geçilebilir.
- 80 altı: revision. Yalnız iç Basecamp revizyon niyeti hazırlanabilir.
- 89.999999 değeri 90'a yuvarlanmaz. Ondalık hesap Decimal ile yapılır.

Puan ne demek?
Puan, kabul edilmiş ve sürümlenmiş rubriğin ağırlıklı uyum toplamıdır; modelin
kendi eminlik yüzdesi veya işin doğru olma ihtimali değildir. Bir modelin
"overall_score: 95" demesi kabul edilmez. Her ölçütün ağırlığı, açıklaması,
brief maddeleri, ölçüm yöntemi ve gözlemi bulunmalıdır; toplam ağırlık 100'dür.
Rubrik ağırlıkları ve yöntemleri bu kod tarafından kullanıcı adına onaylanmaz.

Yanlış marka, eksik zorunlu dosya, yanlış teknik özellik, hak/içerik sorunu ya
da kritik ölçüt hatası yüksek puanla telafi edilmez. Bilinen bir zorunlu hata
revizyon ister. Kontrol edilmemiş ölçüt sıfır puan sayılmaz; puan hesaplanmaz
ve iş tutulur. Hem bilinen zorunlu hata hem eksik başka kontrol varsa yalnız
kanıtlanmış hatanın revizyonu istenebilir. Yetki/güvenlik/devralma/önceki belirsiz
yazım durumu bütün puanlardan önce gelir ve iş tutulur.

Kanıt bağları
Her ölçüm ve insan onayı aynı işe, hizmete, mail zincirine, çıktı sürümüne,
paket SHA-256'sına, dosya manifestine, brief hash'ine, marka kapsamına, gerçek
kaynak mailin parmak izine, alıcı/CC kümesine, hazırlanmış müşteri maili hash'ine
ve global/iş kontrol nesillerine bağlanır. Bunlardan biri değişirse önceki
kalite raporu veya insan onayı kullanılamaz. Ölçüt başına requirement_refs,
kabul edilmiş rubrikteki listeyle tam eşleşir; alakasız brief maddeleriyle
boşluk doldurulamaz. Gözlemde dosya/brief bağı ve incelenen yer bulunmalıdır.

Bu kod görüntüyü gören/formatı ölçen araç değildir. Trusted adapter gerçek
dosya baytlarını okumalı, ölçmeli ve gerekirse içeriği değerlendirmelidir.
Dosya ya da model metnindeki verified:true alanını kopyalamak doğrulama sayılmaz.
Kritik ölçütte hangi yöntemlerin kabul edildiği de rubrikte açıktır; örneğin
haklar kontrolünde yalnız deterministic/human_review seçilmişse model_rubric
sonucu bu kontrol yerine geçemez. Factual/semantic yöntemlerin gerçek başarı
oranı ayrıca değerlendirilecek; bu modül kaliteyi kendiliğinden kanıtlamaz.

Yapılandırma
service.delivery.release_mode == 'scored_quality_v1' olduğunda yeni yol seçilir.
service.delivery.quality_policy tam, kabul edilmiş sürümlü rubriktir. Eksik
veya hatalıysa eski qa.accepted bayrağına geri dönülmez. release_mode belirtilmezse
eski üretici/teslim serbest bırakma yolu aynen korunur.

Önemli quality_policy alanları:
  schema_version, policy_id, version, brand_id, service_id, status='accepted',
  verified, evidence_ref, accepted_at, expires_at, approval_actors,
  assessment_max_age_seconds, approval_max_age_seconds,
  trusted_evaluators={deterministic:[],human_review:[],model_rubric:[]},
  critical_checks, critical_requirements={id:{requirement_refs:[],methods:[]}},
  criteria=[{id,description,weight,critical,methods,requirement_refs}],
  thresholds={human_min:'80',candidate_min:'90'}, internal_revision_limit.

Marka kapsamı bu rubriği zaten içerdiği için rubriğin içine aynı scope_digest
yeniden gömülmez (döngüsel hash oluşur). Tam kapsam hash'i değerlendirme bağında
bulunur; rubrik değişikliği hem policy_digest hem scope_digest değerini değiştirir.

Koordinatör bağlantısı
Üreticinin açık, doğrulanmış dosya sunumu (producer_submission) hâlâ gereklidir.
Bu, "dosya üretildi" kanıtıdır; 90+ için Özge/Ahmet/Cansu adına uydurulmuş
müşteriye gönderim onayı oluşturulmaz. Orta banttaki kalite onayı ayrı kayıttır.
Güncel marka/hizmet kapsamındaki yetkili isim/e-posta listesi esas alınır.

Trusted snapshot ek alanları:
  quality_assessment, quality_safety, quality_human_approval (varsa),
  quality_assets_ref.
Koordinatör bunları, quality_submission, quality_brief_sha256,
quality_manifest_sha256 alanlarıyla gönderim isteğine aktarır. Hem koordinatör
hem autonomy_policy aynı build_quality_binding yardımcısını kullanır. İnsan
onayı ve kalite kapısı son gönderim kontrolünde tekrar değerlendirilir.

quality_safety alanı güncel adapter kanıtıdır: exact binding, verified,
evidence_ref, observed_at, control='active', authority='allowed', security='clear',
write_state='new'. 60 saniyeden eski, eşleşmeyen veya belirsiz güvenlik kaydı
revizyon yorumunu dahi açmaz. Bu, Hamle'nin görmediği CC'den çıkarılma maili
sorununu çözmez; mevcut gecikmiş iş yetkisi denetimi aynen sürer.

Puan notu ve döngü sınırı
Sonuç ve ölçüt dökümü Türkçe bir quality_note olarak hazırlanır; revizyon
gerekiyorsa yalnız müşteri görünürlüğü kapalı projeye add_comment niyeti olur.
90+ veya gerçek insan onaylı 80–89 adayda iki ayrı kalıcı adım vardır:
  1. quality_note_pending: Basecamp kalite notu için tek add_comment niyeti.
  2. quality_noted: yorumun tam sağlayıcı geri okuması kesinleşmiş durum.
  3. Yeni güncel kontrol turunda ready_delivery: ancak aynı incelemeye bağlı
     not makbuzu mevcutsa tek müşteri maili niyeti.
Her tur en fazla bir yazım niyeti döner. İlk yorumun started/unknown sonucu
hem yorumu tekrar etmeyi hem mail göndermeyi durdurur. Kesin not revizyon
sayacını artırmaz. Not ve mail ayrı action_id/kalıcı makbuzlarla izlenir.

Not, tam değerlendirme hash'ine, rubriğe, mail zarfına/alıcılarına, iş/dosya/
brief/kapsama, kontrol nesillerine, Basecamp görevine ve varsa insan onayının
tam kaydına bağlıdır. Bunlardan biri değişirse eski not yeni teslimin önünü
açmaz; yeni inceleme ve yeni nota ihtiyaç vardır. İnsan devralması/devralma
sonrasında eski nesil onayı yeniden geçerli sayılmaz. Zaten kesin gönderilmiş
aynı mail için tekrar hesaplanan puan yeni not veya ikinci mail üretmez.

Son müşteri gönderim isteğinin quality_note_proof alanı action_id,
payload_sha256, quality_context, quality_context_sha256 ve kesin sağlayıcı
receipt içerir. Hem koordinatör hem son autonomy_policy kapısı bu kanıtı
güncel bağlamla doğrular. build_quality_note_context ortak bağ üreticisidir.
Makbuzun kendisinde de quality_context_sha256 zorunludur: güvenilir yürütücü
gerçek sağlayıcı geri okumasını, yazım öncesinde kalıcılaştırılmış değişmez
niyet/kuyruk kaydına bağlayarak makbuzu üretir. Google/Basecamp'in bu alanı
kendiliğinden döndürdüğü varsayılmaz. Eski makbuz yeni teslim için sarılırken
bağlam alanı sonradan eklenemez/değiştirilemez; record_outcome eksik veya başka
bağlamdaki makbuzu reddeder. Aynı bağ son gönderim kapısında tekrar eşleşmelidir.
Bir yorumun varlığı, güncel yetki veya insan onayının yerine geçmez. İnsan
devralması ve gecikmiş iş yetkisi her iki aksiyon öncesinde yeniden denetlenir.

Bu sıra yerel koordinatör ve kalıcı durum sözleşmesinde uygulanmıştır; gerçek
Basecamp/Gmail yürütücüsüyle canlı kabul yapılmamıştır. Niyet veya yerel
makbuz testi, gerçek Basecamp'e yazıldı ya da müşteriye gönderildi demek değildir.

İç kalite düzeltmeleri müşterinin sözleşmedeki revizyon turlarıyla aynı sayaç
sayılmaz. quality_revision_count yalnız doğrulanmış iç yorum sonrası artar;
customer revision_index değiştirilmez. Yeni bir revizyon için rubrikte açık
internal_revision_limit (1–10) bulunmalıdır. Bu sınır dolarsa insan incelemesi
gerekir; limitsiz model/ekip ping-pong'u başlatılmaz. Aynı kesinleşmiş yorum,
sınır dolmuş olsa da tekrar yazılmaz. Sınır değeri kullanıcı adına icat edilmez.

Dosyalar ve doğrulama
- v11_quality_gate.py: saf hesap/kanıt/insan onayı kapısı.
- test_v11_quality_gate.py: 43 saf test.
- test_v11_scored_delivery.py: 37 koordinatör bağlantı testi.
- v11_delivery.py: yalnız açıkça seçilen scored_quality_v1 yolu eklendi.

../hamle-gmail-connector/.venv/bin/python -m unittest \
  test_v11_quality_gate test_v11_delivery test_v11_scored_delivery -q
Bu üç dosya 118 test içerir (43 + eski 38 + yeni 37).

Canlıya geçişte hâlâ gerekenler
Gerçek WAT brief/rubrik ve güncel onaylı marka varlıkları; gerçek dosya ve
görsel/video analiz adapterleri; model/insan kalite kalibrasyonu; iç revizyon
sınırının kabulü; doğru proje/kategori/kişi bağları; tüm gecikmiş aksiyonların
güncel yetkisi; kalıcı skor notu+teslim sırasının canlı yürütücü bağlantısı;
canlı/gölge uçtan uca kabul.
Bu sürüm bunları tamamlandı saymaz, otomatik müşteri gönderimini açmaz.
