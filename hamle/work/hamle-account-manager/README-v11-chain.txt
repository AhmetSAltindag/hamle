Hamle v1.1 — iç canlı zincir (OFFLINE, CANLI DEĞİL)
11 Ekim 2026

Dosyalar: v11_chain.py, test_v11_chain.py,
  ../../outputs/gamechangers-internal/gamechangers-internal-catalog.json (TASLAK profil).
Mevcut modüller değiştirilmedi. worker.py bu zinciri çağırmaz; giriş noktası ayrı:
  run_chain_tick(ChainDeps(...))

Bir tur (15 dakikada bir):
1. Global kuyruk kontrolü queue_open değilse: sıfır okuma, sıfır model, sıfır yazım.
   Profilde live_enabled + provider_mutation_allowed_by_this_file true değilse aynı.
2. Mail: doğrulanmış son mesaj, Hamle gerçek To/CC'de, izinli AJANS göndereni
   (_email/_contacts). Uymayan mail modele gitmez. İş = mail zinciri (bind_work).
3. Mail olayı: interpreter (canlıda model.py yolu) -> plan_intake.
   Eksik bilgi: aynı zincirde YALNIZ gönderene clarify_brief maili; görev açılmaz.
   Tam brief: initialize_production -> create_stage_task (profil rotası/üreticisi,
   Özge/Ahmet/Cansu takipçi). Ardından iç bağlam notu (aynı markanın geçmiş işleri +
   modelin takip önerileri) görevin altına AYRI iç yorum olarak yazılır; brand_task
   açıklaması profil-türevli olduğu için değiştirilmedi. Requester'a gitmez.
4. Basecamp: BasecampReader -> PollJournal -> kuyruk olayı. Hamle'nin kendi yorumu
   yok sayılır. classifier: question / submission / approval / other.
   Soru: record_producer_question; answerer brief/profile dayanaklı cevap verirse
   cevap yorumu, yoksa Özge/Ahmet/Cansu etiketlenir.
   Teslim: assessor kanıtı -> evaluate_quality.
     90+   -> DeliveryCoordinator; mail aynı zincire, alıcı/CC kaynak mail ile aynı.
     80–89 -> onaycılar etiketlenir, beklenir. "ONAY" yazısı yetmez; approval_reader
              doğrulanmış insan onay kaydı döndürmeli.
     <80   -> iç düzeltme yorumu; yalnız doğrulanmış yazım sayılır; 2 turdan sonra
              insana devir (iş takeover moduna geçer).
5. Her yazım: kalıcı niyet (chain kaydı) -> authorize() -> begin_write -> TEK deneme
   -> geri okuma -> confirm_write. Belirsizse hold_unknown, otomatik tekrar yok.
   Tekrarlanan tur aynı işlemi tekrar üretmez (kuyruk tekilleştirme + niyet anahtarı).

Enjekte edilenler: mail_reader, interpreter, classifier, answerer, assessor,
approval_reader, task_writer_factory (BrandTaskWriter), basecamp_get (SessionGet),
comment_writer (BasecampCommentWriter, yeni ve dar), mail_writer, delivery_bridge,
authorize, budget_ok, clock.

Canlı öncesi eksikler: gerçek Todolist kimliği; Cansu/Özge'nin iç projeye üyeliği;
taslak profilin onayı (verified, agreement kuralı); canlı mail_writer (clarify +
teslim, ek dahil); interpreter/classifier/answerer istemleri; iç kalite rubriği
(assessor); delivery_bridge (gerçek dosya baytı, QA, serbest bırakma kaydı);
Basecamp @mention sgid'leri; bind_stage_task olayının aktörü (açık karar).

Test (sentetik, ağ yok): python3 -B -m unittest test_v11_chain -v
