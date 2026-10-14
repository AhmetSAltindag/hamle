Hamle v1.1 — Microsoft Teams v1 (KAPALI, kuruluma hazır)

Dosyalar: v11_teams.py, teams_service.py, teams_mention_prompt.txt, teams-app/manifest.json,
test_v11_teams.py; client-watch-config.json içinde "teams" bloğu (enabled=false);
cloud_providers.py: yeni gizli ad hamle-teams-bot-secret (yalnız ad; değer hiçbir dosyada yok).

Ne yapar
- Ajans içi Teams'te Hamle bir bot. Eklendiği kanal ve grup sohbetlerindeki HER mesajı okur
  (RSC izinleri ChannelMessage.Read.Group ve ChatMessage.Read.Chat; yalnız etiketlenince değil).
  Kaynak: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-with-rsc
- Her mesaj (kişi, metin, dosya adları) konuşma başına bağlam olarak operasyon deposuna yazılır
  (konuşma başına son keep_messages mesaj). teams.conversations: Teams konuşma kimliği -> marka anahtarı
  (Basecamp projesi). Eşlenmemiş konuşma "ajans genel bağlamı" olarak tutulur. Hamle ile birebir sohbet
  "personal" olarak tutulur. Diğer modüller bağlamı v11_teams.teams_context(ops, marka) ile okuyabilir.
- @Hamle diye etiketlenince (ya da birebir yazılınca) tek Claude çağrısı (rota customer_text,
  teams_mention_prompt.txt) dört eylemden birini seçer: answer / ask / cannot / task.
- task: Basecamp işin kaydıdır. Mesajda bir Basecamp to-do bağlantısı varsa o to-do'ya tek iç yorum
  (v11_chain.BasecampCommentWriter); yoksa markanın Sosyal Medya ya da Projeler listesinde yeni to-do
  (v11_client_job.JobTodoWriter, müşteri mailinden iş açan aynı yazıcı; atanan kişi ve "when done"
  kişileri jobs bloğundan). Teams cevabı to-do bağlantısını verir.
  Not: v11_brand_task.create_stage_task doğrulanmış üretim briefi + kuyruk işi ister; bir sohbet
  mesajında bunlar yok. Bu yüzden Teams, marka to-do'su için jobs yazıcısını kullanır (bugün WAT
  Mobilite ve Rabam). Jobs'ta olmayan markada Hamle "açamıyorum, to-do açıp beni etiketleyin" der.
- Cevaplar Bot Connector API ile (aktivitenin serviceUrl'i, yalnız service_hosts) aynı konuya gider.

Kurallar
- Her istekte Bot Framework JWT doğrulanır: login.botframework.com OpenID metadata/anahtarları, RS256,
  issuer https://api.botframework.com, audience = app_id, süre (5 dk tolerans), anahtarda "msteams"
  onayı, serviceurl iddiası = aktivitenin serviceUrl'i. Geçmeyen istek 401, hiçbir şey yazılmaz.
- Yalnız message aktiviteleri ve yalnız bizim tenant. Kanal, grup sohbeti ve Hamle ile birebir sohbet.
  Kişiler arasındaki birebir sohbetler bota hiç gelmez; Hamle Teams'i Graph ile OKUMAZ.
- 'dur' (genel durdurma): durdurulmuşken mesaj ne saklanır ne cevaplanır; kuyruktaki cevap da durur
  (yeni nesil açılsa bile eski cevap yazılmaz).
- Bütçe: iç model tavanı (internal_live_cap) ve aylık bütçe beklemesi her model çağrısından önce;
  tavandaysa model çağrılmaz, sabit kısa cevap gider.
- Sınırlar: konuşma başına günde max_replies_per_conversation_per_day cevap (aşınca bir kez not, sonra
  sessiz); toplamda günde max_model_calls_per_day model çağrısı.
- Tekrar: Bot Framework aynı aktiviteyi yeniden gönderebilir; aktivite kimliği bir kez kaydedilir.
- Her Basecamp yazımından önce kalıcı niyet kaydı; tek deneme, readback; belirsiz sonuç 'unknown',
  otomatik tekrar yok. Teams cevabı da tek deneme.
- v1 sınırları: dosyaların içi okunmaz (yalnız ad/tür); düzenlenen/silinen mesajlar işlenmez; servis
  düşerse kuyruktaki cevap 'queued' kalır (kendiliğinden tekrar yok); Hamle kendiliğinden yazmaz.

AJANSIN MICROSOFT 365 YÖNETİCİSİNİN YAPACAKLARI
1. Entra ID > App registrations > New registration
   Ad: Hamle. Supported account types: "Accounts in this organizational directory only" (single tenant).
   Application (client) ID ve Directory (tenant) ID'yi not edin (gizli değil).
2. Aynı uygulamada Certificates & secrets > New client secret (ör. 12 ay). Değeri yalnız bir kez görünür.
   Değeri mail/Teams/WhatsApp ile GÖNDERMEYİN; Ahmet'le birlikte doğrudan Google Secret Manager'a
   hamle-teams-bot-secret adıyla girilir. Bitiş tarihini takvime yazın (yenilemede yeni sürüm eklenir).
3. Azure portal > Create a resource > Azure Bot
   Bot handle: hamle-gamechangers · Pricing: F0 (Teams standart kanalı ücretsiz) ·
   Type of App: Single Tenant · Creation type: "Use existing app registration" (1. adımdaki App ID + tenant).
   Messaging endpoint: https://<Cloud Run servis adresi>/api/messages (servis kurulunca girilir).
   Channels > Microsoft Teams kanalını ekleyin (Microsoft Teams Commercial), şartları kabul edin.
4. Teams admin center
   - Teams apps > Manage apps > Org-wide app settings: özel (custom) uygulamalara izin verin.
   - Teams apps > Setup policies: yükleyecek kişi(ler) için "Upload custom apps" açık olsun.
   - Teams apps > Permissions (resource-specific consent): ekip ve sohbet sahiplerinin bu uygulamaya
     RSC izni vermesine izin verin (ChannelMessage.Read.Group, ChatMessage.Read.Chat).
5. Uygulama paketi: teams-app/manifest.json içindeki üç 00000000-... değerini (id, bots[0].botId,
   webApplicationInfo.id) 1. adımdaki App ID ile değiştirin; developer adreslerini gerekirse
   ajansın gizlilik/kullanım sayfalarıyla güncelleyin. Yanına color.png (192x192) ve outline.png
   (32x32, saydam zemin beyaz çizgi) koyun; üç dosyayı klasörsüz tek ZIP yapın.
6. Teams admin center > Teams apps > Manage apps > Upload new app ile ZIP'i yükleyin.
7. Yalnız seçilen yerlere kurun: ilgili ekipte Manage team > Apps > Hamle'yi ekleyin (ekip sahibi RSC
   onay ekranını kabul eder); grup sohbetinde + / Apps > Hamle. Kurulmadığı ekip ve sohbetleri Hamle görmez.
8. Kurulan her kanal/sohbet için hangi markaya ait olduğunu Ahmet'e yazın (kanal bağlantısındaki
   19:...@thread... kimliği). Eşlenmeyenler ajans genel bağlamı olur.
9. Aşağıdaki KVKK notunu ekiple paylaşın (kurulumdan önce).

Cloud Run servisi (taslak; deploy Ahmet'in GO'su ile, bu dosya hiçbir şeyi çalıştırmaz)
- İmaj: zincir imajı (package_chain_source.py artık v11_teams.py, teams_service.py,
  teams_mention_prompt.txt dosyalarını da paketler). Ayrı bir SERVİS (job değil):
    ad hamle-teams · bölge europe-west1 · Hamle çalışma servis hesabı
    komut: gunicorn --bind :8080 --workers 1 --threads 4 'teams_service:create_app()'
    --min-instances 1 --max-instances 1 --no-cpu-throttling (cevaplar HTTP 200'den sonra arka planda)
    --allow-unauthenticated (Bot Framework Google kimliği taşımaz; kimlik JWT ile uygulamada doğrulanır)
    ortam: HAMLE_ALLOW_FIRESTORE_OPERATIONS=yes, HAMLE_PROJECT_ID=gamechangers-hamle,
           HAMLE_FIRESTORE_DATABASE=hamle-operations
- Gizliler (yalnız adlar, Secret Manager'dan uygulama okur): hamle-teams-bot-secret (yeni),
  hamle-anthropic-api-key, hamle-basecamp-credential. Servis hesabının bu üçüne erişimi olmalı.
- Sağlık: GET /healthz. Uç: POST /api/messages (Azure Bot messaging endpoint'i).
- Açma sırası: servis -> Azure Bot endpoint -> client-watch-config.json teams: app_id, tenant_id,
  conversations doldurulur, enabled=true yalnız Ahmet'in sözüyle -> imaj yeniden paketlenir.
  enabled=false iken servis 503 teams_disabled döner ve hiçbir şey okumaz.

KVKK notu (ekibe gönderilecek metin)
"Merhaba ekip, Hamle (ajansın yapay zekâ asistanı) Teams'te yalnız eklendiği kanal ve grup
sohbetlerinde bulunacak. Hamle bu kanalları okur: yazılan mesajları, gönderenin adını ve paylaşılan
dosyaların adlarını iş bağlamı olarak ajansın Google Cloud ortamında saklar ve yalnız
iş takibi için kullanır. Mesajlar, Hamle etiketlendiğinde cevap üretmek için yapay zekâ modeline
(Anthropic Claude API) gönderilir. Hamle birebir özel sohbetlerinizi göremez ve okumaz; yalnız
Hamle'ye birebir yazdığınız mesajları görür. Kişisel ya da işle ilgisi olmayan konuşmaları Hamle'nin
eklendiği kanallarda paylaşmayın. Hamle'nin bir kanaldan çıkarılmasını ya da kayıtların silinmesini
Ahmet'ten isteyebilirsiniz. 'dur' komutu Hamle'yi tümüyle durdurur."

Doğrulama (sentetik, ağ yok): python3 -B -m unittest test_v11_teams -q
