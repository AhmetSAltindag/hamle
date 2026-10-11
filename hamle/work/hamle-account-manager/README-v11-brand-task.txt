Hamle v1.1 — marka profiline bağlı üretim görevi (OFFLINE, CANLI DEĞİL)

Dosyalar: v11_brand_task.py, test_v11_brand_task.py (+ v11_intake_plan.py: istenen
çıktılara açıkça belirtilmemişse purpose='final' varsayılanı; üretim aşamaları bunu zorunlu tutuyor).

Zincir (mevcut modüller, yeniden yazım yok):
  plan_intake (doğrulanmış mail + brief + profil rotası)
  -> initialize_production / ready_stages (hangi aşama açılabilir)
  -> prepare_stage_task (profil + brief'ten deterministik Basecamp gövdesi, kuyruk işine bağlı)
  -> create_stage_task: authorize() -> QueueStore.begin_write -> tek POST -> readback
     -> confirm_write -> ilk aşamada bind_task; dönen stage_task bind_stage_task olayına hazırdır.

Kurallar:
- Hedef hesap/proje/liste, üretici ve gözetim kişileri yalnız GÜNCEL profilden türetilir;
  yazıcı aynı türetmeyi tekrar yapar, birebir eşleşmeyen yük reddedilir.
- Profil sürümü/özeti, rota veya üretici değişirse: brand_profile_changed_replan_required.
- Kuyruk olayı brief'in kaynak mailiyle aynı parmak izini taşımalı.
- Profilde live_enabled ve provider_mutation_allowed_by_this_file true değilse hiçbir istek
  atılmaz (WAT bugün kapalı: brand_profile_live_disabled).
- POST öncesi liste aktif, projeye ait ve müşteriye görünmez olmalı.
- Belirsiz POST/readback: hold_unknown, otomatik tekrar yok. notify=false, due_on gönderilmez.
- Görev açıklamasında müşteri e-posta adresi yer almaz.

Açık noktalar: bind_stage_task olayını kimin adına kaydedeceği (Hamle gözetim rolünde değil);
ikinci aşama (ör. Samet) görevi tasarım kabulünden sonra aynı fonksiyonla açılır ama uçtan uca
test edilmedi; Basecamp listesindeki gerçek görev şablonu/etiket gereksinimi doğrulanmadı.

Doğrulama (sentetik, ağ yok): python -m unittest test_v11_brand_task -q
