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

Marka yönlendirmesi (brand routing, 2026-10-11, v11_brand_routing.py)
---------------------------------------------------------------------
Katalog: `brand_routing.enabled` (bayrak; false => her iş iç "Hamle İşleri" listesinde),
`brand_routes` (marka anahtarı -> project_id, todoset_id, lists{social,project,motion},
list_id (= social), verified, clients_enabled, lists_visible_to_clients, hamle_member,
sm_person_id, rep_person_id, aliases, match_scope), `brand_routes_excluded` (Cacaonly).
`basecamp` bloğu (hesap 5898865, Hamle 53358100, iç proje 49184053) ve iç rota değişmedi.

Akış:
- Marka tespiti deterministik: konu + son mail gövdesi (alıntı satırları, e-posta adresleri,
  URL'ler atılır), Türkçe küçük harf, tam kelime alias eşleşmesi. Tam bir marka => o markanın
  rotası; sıfır / birden fazla / dışlanmış (Cacaonly) / kullanılamaz rota => iç rota.
  "Gamechangers" yalnız KONUDA aranır (imzalarda geçtiği için). "ya da" alias değildir.
  Yorumlayıcı (model) marka alanı döndürmüyor; model istemleri değişmedi.
- İş, ilk mailde bağlanır (aynı zincirin sonraki mailleri bağlamı değiştirmez).
  Markalı iş önce markanın Sosyal Medya listesine bağlanır; yorumdan sonra, görev açılmadan
  ÖNCE aynı projenin listesine taşınır (queue.retarget_list): sosyal medya içeriği
  (post/story/reels/carousel...) -> social; diğer animasyon/video -> motion (yoksa project);
  geri kalan ve karışık -> project.
- Görev: atanan = markanın SM'i; tamamlanma takipçileri = SM + rep + Ahmet; notify=false;
  mention'lar yalnız metin. Mevcut BrandTaskWriter korumaları aynen geçerli (hesap, Hamle
  kimliği, liste visible_to_clients=false POST öncesi yeniden okunur, görev geri okunur).
- Kullanılamaz rota (Fenerium, New Biz, doğrulanmamış marka): iç rota, başlıkta "[Marka] ",
  iç bağlam notunda neden + marka sahibinin etiketi.
- İzin listesi = iç rota + kullanılabilir marka rotalarının listeleri. BasecampCommentWriter ve
  ChainAuthorizer (create_task + add_comment) bu listenin dışını reddeder.
- Markalı işlerde kalite/teslim köprüsü (v11_chain_quality) hâlâ iç projeye bağlı: teslim
  yorumları kalite kontrolünde DURUR ve yöneticiler etiketlenir (fail-closed). Görev açma ve
  soru-cevap çalışır.

Bir markayı açmadan önce Mac'te okunacaklar (brand_routes_readback.py, salt okunur):
  BASECAMP_ACCESS_TOKEN=... python3 brand_routes_readback.py [--brand <anahtar>]
  Her marka projesi için:
  1. GET /projects/{project_id}.json -> clients_enabled (bool), dock'taki todoset id.
  2. GET /projects/{project_id}/people.json -> Hamle 53358100, SM, rep ve Ahmet 47470759 üye mi.
  3. GET /buckets/{project_id}/todosets/{todoset_id}/todolists.json -> social/project(/motion)
     listelerinin id'si, status=active ve visible_to_clients=false.
  Hepsi tamamsa katalogda: lists, list_id (= social), todoset_id, clients_enabled,
  lists_visible_to_clients=false, hamle_member=true, verification_ref, verified=true.
  Biri eksikse verified=false kalır (iç rotaya düşer). clients_enabled=true projelerde
  (HAN Spaces, ya da) liste client'a görünürse rota kullanılmaz.
  İsteğe bağlı yazım (tek marka, iki bayrak birden):
  python3 brand_routes_readback.py --brand <anahtar> --create-hamle-list --confirm-create
  -> o projenin todoset'inde "Hamle İşleri" listesi açar (yoksa), geri okur; visible_to_clients
  false değilse kullanılmaz. Testler bu betiği ağ ile çalıştırmaz.
Kapatma: katalogda brand_routing.enabled=false (veya markanın verified=false).
