import fs from 'node:fs/promises';
import assert from 'node:assert/strict';

const setupPath = 'outputs/gamechangers-pilot/current-setup.json';
const connectionPath = 'outputs/Hamle_Kalici_Baglanti_Hazirlik_2026-10-09.json';
const uiPath = 'outputs/Hamle_Baglanti_Durumu_2026-10-09.json';
const setup = JSON.parse(await fs.readFile(setupPath, 'utf8'));
const connection = JSON.parse(await fs.readFile(connectionPath, 'utf8'));
const ui = JSON.parse(await fs.readFile(uiPath, 'utf8'));
const source = (path, pointer) => ({ path, pointer });
const requested = [
  ['wat_mobilite', 'WAT Mobilite', 'wat_mobilite'], ['rabam', 'Rabam', 'rabam'],
  ['ya_da', 'ya da', 'yada_multicosmetics'], ['multicosmetics', 'Multicosmetics', 'yada_multicosmetics'],
  ['altinmarka', 'Altınmarka', 'altinmarka'], ['bikaldi', 'Bikaldi', 'bikaldi'],
  ['han_spaces', 'HAN Spaces', 'han_spaces'], ['gamechangers_internal', 'Gamechangers', 'gamechangers_internal'],
];
const brands = requested.map(([id, name, registryId]) => {
  const index = setup.brand_registry.findIndex(x => x.brand_id === registryId);
  assert(index >= 0);
  const original = setup.brand_registry[index];
  const pointer = `/brand_registry/${index}`;
  const combined = registryId === 'yada_multicosmetics';
  return {
    brand_id: id,
    brand_name: name,
    relationship: original.relationship,
    status: id === 'gamechangers_internal' ? 'internal_test_connection_verified_production_inputs_pending' : 'pending_verified_brand_inputs',
    production_ready: false,
    live_enabled: false,
    source_registry_id: registryId,
    source_record: source(setupPath, pointer),
    identity_note: combined ? 'Önceki kayıt ya da / Multicosmetics / Remedium başlığı altında birleşik. Bu hazırlıkta istenen iki marka ayrı tutuldu; tüzel kişi, hizmet ve varlık sınırı doğrulanmadan ortak yetki veya proje atanmaz.' : null,
    user_supplied_domains: original.user_supplied_domains,
    domain_source: source(setupPath, `${pointer}/user_supplied_domains`),
    domains_grant_recipient_authority: false,
    authorized_client_people: original.authorized_client_people,
    agency_owner_id: original.agency_owner_id,
    production_team_ids: original.production_team_ids,
    allowed_recipients: original.allowed_recipients,
    service_scope_record_id: original.service_scope_record_id,
    revision_rules_record_id: original.revision_rules_record_id,
    delivery_rules_record_id: original.delivery_rules_record_id,
    authority_limits_record_id: original.authority_limits_record_id,
    production_basecamp: {
      account_id: original.basecamp_account_id,
      project_id: original.basecamp_project_id,
      task_list_ids: original.basecamp_task_list_ids,
      customer_visibility: original.customer_visibility,
    },
    approved_brand_assets: {
      source_locations: original.source_locations,
      current_guideline_record_id: null,
      approved_assets_record_id: null,
      usage_rights_record_id: null,
      approving_person_id: null,
    },
    historical_learning_refs: original.learning_sources.map(p => `outputs/gamechangers-pilot/${p}`),
    historical_learning_is_current_authority: false,
    hard_gates: [
      { id: 'A04', status: 'not_met', required: ['authorized_client_people', 'agency_owner_id', 'allowed_recipients', 'service_scope_record_id', 'revision_rules_record_id', 'delivery_rules_record_id', 'authority_limits_record_id'], source: source(setupPath, pointer) },
      { id: 'B10', status: 'not_met', required: ['production_basecamp.account_id', 'production_basecamp.project_id', 'production_basecamp.task_list_ids', 'production_basecamp.customer_visibility'], source: source(setupPath, pointer) },
      { id: 'C05', status: 'not_met', required: ['approved_brand_assets.source_locations', 'approved_brand_assets.current_guideline_record_id', 'approved_brand_assets.approved_assets_record_id', 'approved_brand_assets.usage_rights_record_id', 'approved_brand_assets.approving_person_id'], source: source('outputs/gamechangers-pilot/templates/brand-dossier.template.json', '/sections/logos_fonts_colors_assets_templates') },
    ],
    pending_inputs: [
      { task: 'A04', request: 'Yetkili müşteri kişilerinin adresleri ve konu bazlı onay sınırları, ajans iş sahibi, izinli alıcılar, hizmet/revizyon/teslim ve ticari yetki kaydı.', answer: null },
      { task: 'B10', request: 'Bu markaya ve açılacak hizmete ait Basecamp hesap/proje/liste, üyeler ve müşteri görünürlüğü kaydı.', answer: null },
      { task: 'C05', request: 'Güncel marka kılavuzu, onaylı logo/font/renk/şablon ve iş örneklerinin ana konumu; sürüm, kullanım hakkı ve onay sahibi.', answer: null },
      ...(combined ? [{ task: 'A04', request: 'ya da ile Multicosmetics arasındaki marka/tüzel kişi, hizmet, onay, proje ve varlık sınırları.', answer: null }] : []),
    ],
    internal_test_connection: null,
  };
});
const internal = brands.find(x => x.brand_id === 'gamechangers_internal');
const bc = connection.basecamp_oauth;
assert.equal(bc.initial_test_project_id, '49184053');
assert.equal(bc.person_id, '53358100');
assert.equal(bc.user_email, 'hamle@gamechangers.com.tr');
assert.equal(bc.write_readback_verified, true);
internal.internal_test_connection = {
  scope: 'Hamle — İç Deneme bağlantı testleri. Müşteri üretim yetkisi değildir.',
  account_id: bc.account_id,
  project_id: bc.initial_test_project_id,
  actor_person_id: bc.person_id,
  actor_email: bc.user_email,
  task_list_ids: null,
  visible_to_clients: bc.visible_to_clients,
  read_and_write_readback_verified: bc.write_readback_verified,
  refresh_tested: bc.token_refresh_tested,
  source_fields: {
    account_id: source(connectionPath, '/basecamp_oauth/account_id'),
    project_id: source(connectionPath, '/basecamp_oauth/initial_test_project_id'),
    actor_person_id: source(connectionPath, '/basecamp_oauth/person_id'),
    actor_email: source(connectionPath, '/basecamp_oauth/user_email'),
    visible_to_clients: source(connectionPath, '/basecamp_oauth/visible_to_clients'),
    read_and_write_readback_verified: source(connectionPath, '/basecamp_oauth/write_readback_verified'),
    refresh_tested: source(connectionPath, '/basecamp_oauth/token_refresh_tested'),
  },
};
const result = {
  schema_version: '1.0.0', artifact_kind: 'brand_onboarding_readiness_local_preparation',
  as_of: '2026-10-09', timezone: 'Europe/Istanbul',
  scope: ['A04', 'B10', 'C05'],
  creates_or_enables_external_actions: false,
  source_review: { provider_revalidation: false, raw_email_bodies_read: false, credentials_read: false, local_config_and_learning_metadata_only: true },
  source_precedence: '9 Ekim gerçek iç bağlantı kanıtı, 8 Ekim şablonunun eski bağlantı durumundan üstündür. Eski kişi/proje boşlukları doğrulanmış değer yokken null kalır.',
  rules: [
    'Her marka hedef kapsamda kalır. Hazır markanın açılması eksik diğer markalara bağlanmaz.',
    'Tarihsel yazışmada ad, gönderici alanı veya tek iş onayı görülmesi kalıcı onay ve alıcı yetkisi değildir.',
    'Son ilgili mesajın gerçek To/CC alanında tam Hamle adresi gerekli ama tek başına yeterli değildir; marka, kişi, hizmet ve araç yetkileri ayrıca gerekir.',
    'Hamle bulunmayan içerikten yeni iş, revizyon, gönderim veya Basecamp aksiyonu başlatılmaz. Görülmeyen çıkarılma mesajı algılanmış sayılmaz.',
    'İç test projesi ve test muhatapları müşteri üretim alanlarına kopyalanmaz.',
    'A04/B10/C05 portföy geneli tamamlanmasıyla marka-hizmet alt kabulü ayrıdır. İlk sınırlı canlı için ayrıca IN13 ve diğer canlı koşulları gerekir.',
    'Bu dosya gerekli alanları hazırladı; A04/B10/C05 tamamlandı veya herhangi bir marka canlıya hazır anlamına gelmez.',
  ],
  shared_routing_decision_owners: { value: setup.authority.named_decision_owners, source: source(setupPath, '/authority/named_decision_owners'), grants_brand_authority: false },
  internal_mail_test_allowlist: { value: setup.test_environment.email_recipient_allowlist, scope: setup.test_environment.allowlist_scope, source: source(setupPath, '/test_environment/email_recipient_allowlist'), production_allowlist: false },
  brands,
};
function resolvePointer(d, pointer) { return pointer.split('/').slice(1).reduce((v, k) => v?.[k.replace(/~1/g, '/').replace(/~0/g, '~')], d); }
const cache = new Map();
let sourceCount = 0;
async function verifySources(x) {
  if (!x || typeof x !== 'object') return;
  if (typeof x.path === 'string' && typeof x.pointer === 'string') {
    if (!cache.has(x.path)) cache.set(x.path, JSON.parse(await fs.readFile(x.path, 'utf8')));
    assert.notEqual(resolvePointer(cache.get(x.path), x.pointer), undefined, `${x.path}#${x.pointer}`);
    sourceCount++;
  }
  for (const value of Object.values(x)) if (value && typeof value === 'object') await verifySources(value);
}
await verifySources(result);
assert.equal(brands.length, 8);
assert.equal(new Set(brands.map(b => b.brand_id)).size, 8);
assert(brands.every(b => !b.live_enabled && b.authorized_client_people === null && b.production_basecamp.project_id === null));
result.validation = { brand_count: brands.length, source_pointers_resolve: true, source_pointer_count: sourceCount, no_production_authority_inferred: true, completed_scope_tasks: [] };
await fs.writeFile('work/hamle-workflow/brand-readiness.json', JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({ path: 'work/hamle-workflow/brand-readiness.json', brands: brands.length, source_pointers_checked: sourceCount, production_ready: 0, internal_test_project: internal.internal_test_connection.project_id }));
