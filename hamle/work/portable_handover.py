#!/usr/bin/env python3
"""Prepare a private, inspectable Hamle handover; never contacts a provider.

Two steps: --plan records an exact allowlist after scanning permitted source
areas; --create copies only those exact hashed files. Existing snapshots are
never overwritten. Credentials, personal profiles and binaries are not read.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "portable-handover-20261010"
PLAN = ROOT / "outputs" / (NAME + "-allowlist.json")
TARGET = ROOT / "outputs" / NAME
ARCHIVE = ROOT / "outputs" / (NAME + ".zip")
WORK_DIRS = (
    "hamle-account-manager", "hamle-cloud-probe", "hamle-gmail-connector",
    "hamle-internal-intake", "hamle-review-control", "hamle-runtime",
    "hamle-workflow", "project-tracker",
)
DATA_DIRS = ("handoff", "outputs/gamechangers-pilot",
             "outputs/hamle-yeni-mac-devir-2026-10-10", "outputs/new-mac-2026-10-10")
ALLOWED_SUFFIXES = {".py", ".mjs", ".json", ".txt", ".html", ".yaml", ".yml", ".md"}
ALLOWED_NAMES = {"Dockerfile", ".dockerignore", ".gitignore"}
EXCLUDED_DIRS = {"private-connections", ".venv", "venv", "node_modules", "cloud-tools",
                 ".git", "__pycache__", ".pytest_cache", "cache", ".cache",
                 "downloads", "attachments", "raw-mail", "raw_mail"}
EXCLUDED_NAMES = {"gmail-desktop.json", "credentials.json", "token.json",
                  "application_default_credentials.json", "client_secret.json"}
ORIGINAL_NAMES = (
    "Hamle_Proje_Plani_2026-10-10_Yeni_Mac_Devir.docx",
    "Hamle_Proje_Takip_2026-10-10_Yeni_Mac_Devir.xlsx",
)
SECRET_PATTERNS = (
    ("api_key_shape", re.compile(rb"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{25,}")),
    ("google_client_secret_shape", re.compile(rb"GOCSPX-[A-Za-z0-9_-]{16,}")),
    ("google_access_token_shape", re.compile(rb"ya29\.[A-Za-z0-9_.-]{20,}")),
    ("google_refresh_token_shape", re.compile(rb"1//[A-Za-z0-9_-]{30,}")),
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_value", re.compile(rb"(?i)Bearer [A-Za-z0-9_.-]{32,}")),
    ("jwt_value", re.compile(rb"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}")),
    ("signed_url", re.compile(rb"(?i)[?&](?:X-Goog-Signature|X-Amz-Signature|access_token|refresh_token|client_secret)=[^\s\"<>]{12,}")),
    ("oauth_callback_code", re.compile(rb"[?&]code=4(?:%2F|/)[^\s\"<>]{20,}")),
)
CREDENTIAL_FIELDS = {"access_token", "refresh_token", "client_secret", "private_key",
                     "api_key", "openai_api_key", "password", "id_token"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def protected_name(path):
    return (any(part in EXCLUDED_DIRS or part.startswith(".env") for part in path.parts)
            or path.name in EXCLUDED_NAMES
            or path.name.startswith(("credentials-", "client_secret_")))


def iter_allowed(area):
    """Bounded traversal of explicitly named project folders, never profiles."""
    for base, dirs, names in os.walk(area, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and
                          not Path(base, d).is_symlink())
        for name in sorted(names):
            path = Path(base, name)
            if protected_name(path.relative_to(ROOT)):
                continue
            if path.suffix in ALLOWED_SUFFIXES or path.name in ALLOWED_NAMES:
                yield path


def candidates():
    selected = {ROOT / "README_TR.txt", ROOT / "MANIFEST.json", ROOT / ".gitignore"}
    for name in WORK_DIRS:
        selected.update(iter_allowed(ROOT / "work" / name))
    selected.update(path for path in (ROOT / "work").glob("*.py") if not path.is_symlink())
    for rel in DATA_DIRS:
        selected.update(iter_allowed(ROOT / rel))
    selected.update((ROOT / "outputs").glob("Hamle_*.json"))
    for path in sorted(selected):
        yield path, "hamle/" + path.relative_to(ROOT).as_posix(), "project"
    for name in ORIGINAL_NAMES:
        choices = (Path.home() / "Desktop" / "Hamle" / name, Path.home() / "Desktop" / name)
        existing = next((p for p in choices if p.is_file() and not p.is_symlink()), None)
        if existing:
            yield existing, "original-handover/" + name, "original_document"


def scan_text(raw, suffix):
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(raw):
            return label
    if suffix == ".json":
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            return "invalid_json"

        def inspect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).casefold() in CREDENTIAL_FIELDS and isinstance(child, str) and child.strip():
                        return True
                    if inspect(child):
                        return True
            elif isinstance(value, list):
                return any(inspect(child) for child in value)
            return False
        if inspect(data):
            return "credential_field_value"
    return None


def read_and_scan(path, kind):
    if kind == "project" and not path.resolve().is_relative_to(ROOT.resolve()):
        return None, "source_outside_project"
    if kind == "original_document" and (path.name not in ORIGINAL_NAMES or path.parent not in
            (Path.home() / "Desktop" / "Hamle", Path.home() / "Desktop")):
        return None, "original_document_not_allowlisted"
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 16_000_000:
        return None, "not_bounded_regular_file"
    raw = path.read_bytes()
    if kind == "original_document":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 32_000_000:
                return None, "oversize_document"
            for item in archive.infolist():
                if item.filename.endswith((".xml", ".rels")):
                    reason = scan_text(archive.read(item), ".xml")
                    if reason:
                        return None, reason
                elif item.filename != "docProps/thumbnail.jpeg":
                    return None, "unreviewed_embedded_document_binary"
    else:
        try:
            raw.decode("utf-8")
        except UnicodeError:
            return None, "not_utf8_text"
        reason = scan_text(raw, path.suffix)
        if reason:
            return None, reason
    return raw, None


def private_write(path, raw):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600)


def prepare_plan():
    entries, omitted = [], []
    for path, destination, kind in candidates():
        raw, reason = read_and_scan(path, kind)
        if reason:
            omitted.append({"path": destination, "reason": reason})
            continue
        entries.append({"source": str(path), "path": destination, "kind": kind,
                        "bytes": len(raw), "sha256": digest(raw)})
    result = {"schema_version": 1, "prepared_at": datetime.now(timezone.utc).isoformat(),
              "exact_file_allowlist": entries, "omitted_for_review": omitted,
              "network_calls": 0, "paid_calls": 0,
              "excluded_categories": sorted(EXCLUDED_DIRS | EXCLUDED_NAMES) +
                  ["all .env files", "binary screenshots", "raw logs", "archives", "SQLite/WAL/SHM", "secret-bearing files"]}
    # This is preparation metadata, not an immutable handover snapshot.
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    PLAN.chmod(0o600)
    return {"status": "allowlist_prepared", "files": len(entries),
            "source_bytes": sum(v["bytes"] for v in entries),
            "omitted_for_review": omitted, "plan": str(PLAN),
            "snapshot_created": False, "archive_created": False}


README = """HAMLE — ÖZEL TAŞINABİLİR DEVİR / 10 EKİM 2026

Bu klasör ve ZIP, Gamechangers'a ait mevcut özel proje malzemelerinin yerel
anlık kopyasıdır. Yalnız yetkili kişi veya hizmetlerle paylaşın. Dosyalar 0600,
klasörler 0700 izniyle kaydedildi; ZIP şifreli değildir.

ÖNCE OKUNACAKLAR
1. HANDOVER_STATUS.json: bu yeni paketin kapsamı, açıklar ve canlı/yerel farkı.
2. RESTORE_AND_PROVIDER_CHANGE_TR.txt: kurulum, iş kayıtlarını koruma ve model değiştirme.
3. hamle/outputs/new-mac-2026-10-10/ içindeki en yeni özel kanıtlar.
4. MANIFEST.json: dosya karmaları, kapsam ve hariç tutulanlar.
Güncel hazırlık durumu için aynı kanıt klasöründeki
hybrid-preparation-current-status.json önce okunur; eski genel kayıtlar
bu dosyanın ve daha yeni özel kanıtların önüne geçirilmez.

Sol/medium ile beş sentetik gerçek API çağrısı tamamlandı; müşteri verisi
gönderilmedi ve müşteri aksiyonu yapılmadı. Otomatik yönlendirme kontrolü
5/5, beş örneğe ait ön yapay zekâ anlam incelemesi 39/40 oldu. Bu küçük
rubrik sonucu genel doğruluk oranı değildir. İnsan kabulü ve geniş
müşteri kabulü tamamlanmış değildir. Canlı V7 hâlâ Astra/max kullanır.
Yerel hibrit hazırlığın 319 testi geçti; bu kod buluta dağıtılmadı.

hamle/README_TR.txt, eski handoff/GUNCEL_DURUM_2026-10-10.json ve
original-handover/ içindeki Word/Excel dosyaları ilk Mac devrinin tarihsel
belgeleridir. Yeni bulut/API/gönderim kanıtının önüne konmamalıdır.
65 işin ağırlıkları korunur. Yüzde; kalite, süre veya canlı bağımsızlık oranı değildir.

İÇERİK
Kaynak kodu, testler, rol talimatları, öğrenme özetleri, takip planı, özgün
Word/Excel belgeleri, dağıtım/zamanlama kodu ve güvenli işlem kanıtları bulunur.
Öğrenme özetleri şirket/müşteri bilgisi içerebilir. Tam ham mail arşivi ve
mail ekleri bu projede mevcut değildir ve pakette bulunmaz. Eski 7 birleşik
ya da/Multicosmetics/Remedium konusunun güncel markalara eşlenmesi açıktır.
Bu paket bütün sohbetin dışa aktarımı değildir; erişilebilen proje dosyaları
ve karar/işlem kayıtlarının devridir. Eski teknik manifestteki 60 dosyanın
tamamı, bunlara ek yeni özel kaynak, test ve kanıt dosyaları korunmuştur.

Sırlar, tokenlar, OAuth istemci sırrı, API anahtarları, .env, macOS anahtar
kasası, SDK profili, sanal ortam, Basecamp ikili aracı, ekran görüntüleri,
ham günlükler ve SQLite veritabanları aktarılmadı. Bunların yokluğu kasıtlıdır.
Manifestte secret_scan yalnız otomatik taramanın kapsamını açıklar; tüm olası
sır biçimlerine karşı matematiksel güvence değildir. Şüpheli dosya içerikleri
yazdırılmadan dışarıda bırakılır ve OMITTED_FILES.json içinde yalnız yolu yazılır.

NE TAŞINIR / NE YENİDEN GEREKİR
Hamle'nin iş kuralları, Gmail/Basecamp iş akışı ve kayıt tasarımı yeni modelde
yeniden kullanılır. Diğer sağlayıcıya hesap erişimi, sağlayıcı adaptörü,
maliyet hesabı ve gerçek kalite kabulü gerekir. Claude bağlantısı/testi
yapılmadı. Kaynak taşınabilirliği hazır başka sağlayıcı hizmeti anlamına gelmez.
Yeni bir model seçmek müşteri göndermeyi veya yeni bütçeyi otomatik açmaz.

Arşivi doğrulamak için bu klasörde: python3 verify_manifest.py
Bu komut ağ, model, Gmail, Basecamp veya bulut işlemi yapmaz.
"""

RESTORE = """YENİ ORTAMDA DEVAM / SAĞLAYICI DEĞİŞİKLİĞİ

1. Aynı klasör düzenini koruyun. Paket kökünden 'cd hamle' yapın. Python
   3.12, mevcut Cloud Dockerfile sürümüdür. Eski /Users/... yollarını yeni
   makinenin yolu olarak kullanmayın. Kaynaklardaki eski yollar kanıt veya
   yerel araç bağlamı olabilir; çalıştırmadan önce platforma göre doğrulayın.

2. İzole ortamı yeniden oluşturun (ağdan kütüphane indirir, API çağırmaz):
   python3.12 -m venv work/hamle-gmail-connector/.venv
   work/hamle-gmail-connector/.venv/bin/python -m pip install --index-url https://pypi.org/simple --only-binary=:all: -r work/hamle-gmail-connector/requirements.txt -r work/hamle-account-manager/requirements-cloud.txt
   work/hamle-gmail-connector/.venv/bin/python -m pip check
   cd ..
   hamle/work/hamle-gmail-connector/.venv/bin/python -B run_offline_tests.py
   Bu testler bağlantı/teslim kabulünü yeniden üretmez; gönderim yapmaz.

   Belge üretimi isteğe bağlıdır: Word için python-docx, Excel kontrolü için
   openpyxl; Excel üretimi için Node.js ve build_tracker.mjs'nin beklediği
   @oai/artifact-tool ortamı gerekir. Bu ortamı rastgele npm paketiyle taklit
   etmeyin. Hazır DOCX/XLSX pakette ayrıca var. Eski belgeden güncel plan
   üretirken en yeni kabul kanıtını işleyin; eski yüzdeyi güncel diye sunmayın.

3. Yalnız kaynakları açmak veya testleri çalıştırmak canlı bağlantı kurmaz.
   Gmail'de mevcut Desktop OAuth istemcisi ve Hamle kimliği kullanılır;
   yetkiler tam gmail.readonly + gmail.send. Başlangıçta kimlik/kapsam ve
   bilinen iç test kaydı salt okunur doğrulanır. Basecamp için resmi CLI
   kurulumu handoff/Basecamp_Kurulum_Notu.json içindedir; yeni işletim
   sistemine uygun sürüm/hash doğrulanır. CLI yolu work/basecamp-tools/bin/basecamp.
   Cloud wrapper work/cloud_gcloud.py mevcut proje gamechangers-hamle ile
   resmi SDK'yı work/cloud-tools/google-cloud-sdk/bin/gcloud yolunda bekler.
   Kimlik bilgileri özel konuma/işletim sistemi kasasına yeniden bağlanır.
   İstemci sırları, anahtarlar veya tokenlar dokümana/koda/sohbete yazılmaz.

4. SAĞLAYICI DEĞİŞTİRİRKEN: Mevcut validate_analysis sözleşmesini, yetki
   kontrollerini, alıcı kontrolünü ve tek iş/action kimliklerini koruyun.
   Yeni adaptör yalnız model isteğini/cevabını dönüştürür; Gmail ve Basecamp
   yetkisi veya yazma kararı modele verilmez. Yeni model/effort/API erişimi,
   şema, Türkçe iletişim, maliyet ve riskli senaryolar değerlendirilir.
   Belirsiz başarısız çağrı/gönderim başka modelle otomatik yinelenmez.
   Provider/model/usage/price/cost kayıtları yeni sağlayıcıya göre uyarlanır.
   Google Cloud barındırması da değişecekse Firestore transaction/lease/dedup
   semantiği yeni veritabanında ayrıca uygulanır ve kabul edilir.

5. CANLI İŞ KAYITLARI SIFIRLANMAZ. Öncelikli dosya
   hamle/outputs/new-mac-2026-10-10/portable-business-state-post-eval.json
   dosyasıdır; beş Sol denemesi sonrasındaki bütçe ayırmasını da taşır.
   portable-business-state.json daha eski, Sol öncesi tarihsel kopyadır.
   En yeni snapshot zamanını ve kapsamını kontrol edin. Kaynak + durum dışa aktarımı
   tek başına doğrulanmış geri yükleme aracı değildir. Üretim devrinden önce
   eski işçi/zamanlayıcı durdurulur; sürmekte olan provider çağrılarının
   sonucu belirlenir; son iş, event, action, model attempt, bütçe hold ve
   gönderim fişleri tutarlı ve güncel snapshot ile taşınır. İş kimlikleri,
   provider receipt kimlikleri ve dedup hashleri korunur. Eski iki Gmail
   .send-test-attempts dosyası da korunur. İzin süresi geçmiş onaylar
   canlandırılmaz; yeni ortama eski lease/lock taşınarak gönderim yapılmaz.
   'unknown' gönderimler salt okunur provider kayıtlarıyla uzlaştırılır;
   otomatik tekrar yoktur. İlk kontrol mevcut Gmail/Basecamp fişini okumaktır.
   Yeni ve eski işçi aynı iş üstünde eşzamanlı açılmaz. Bu paket üretim
   veritabanına kendiliğinden import veya aktif deploy yapmaz.

6. BULUT DOSYALARI CANLI PROJEYE BAĞLIDIR. deploy, release, provision,
   import_cloud_credentials, seed_cloud_state, scheduler ve live_once
   betikleri offline test komutu değildir. Bunları çalıştırmak yetki,
   maliyet veya provider yazması oluşturabilir. Önce somut hedef/kapsam,
   geçerli bütçe ve servis hesapları doğrulanır. Mevcut kaynaklar tekrar
   oluşturulmaz. Cloud Run/Firestore/Secret Manager/Scheduler yapılandırma
   kodu ve kanıtları bu pakette; sır değerleri ve SDK oturumu hariçtir.

7. Müşteri kapsamı kapalı kalır: marka-proje-yetkili kişi-hizmet-revizyon
   kaydı ve onaylı dosyalar tamamlanır, gölge çalışma ve kabul geçer. Hamle
   gerçek son mailin To/CC alanında olmalı. Hamle'ye hiç ulaşmayan CC çıkarma
   yeni sağlayıcıyla çözülmüş sayılmaz. Görünür durdurma/devralma ve gecikmiş
   işte güncel yetki kontrolü gerekir. Strateji/şikâyet Cansu+Ahmet,
   ticari karar Ahmet, sosyal gündem/plan Özge+Hamle'dir. Özge'ye danışılmadan
   danışılmış gibi yazılmaz. Paket yeni müşteriye gönderim izni değildir.
"""

VERIFY = '''#!/usr/bin/env python3
"""Verify the exported files without reading credentials or using a network."""
from pathlib import Path
import hashlib, json
root = Path(__file__).resolve().parent
manifest = json.loads((root / "MANIFEST.json").read_text())
bad = []
for item in manifest["files"]:
    path = root / item["path"]
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        bad.append(item["path"]); continue
    raw = path.read_bytes()
    if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
        bad.append(item["path"])
print(json.dumps({"status": "verified" if not bad else "failed", "checked": len(manifest["files"]), "failed_paths": bad}))
raise SystemExit(bool(bad))
'''

TESTS = '''#!/usr/bin/env python3
"""Run only synthetic/unit test entry points. Never run live/deploy scripts."""
from pathlib import Path
import os, subprocess, sys
root = Path(__file__).resolve().parent / "hamle"
suites = ["hamle-gmail-connector", "hamle-workflow", "hamle-runtime", "hamle-cloud-probe", "hamle-internal-intake", "hamle-review-control", "hamle-account-manager"]
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
for name in suites:
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"], cwd=root / "work" / name, env=env)
    if result.returncode: raise SystemExit(result.returncode)
raise SystemExit(subprocess.run([sys.executable, "-B", str(root / "outputs/gamechangers-pilot/offline/run_tests.py")], cwd=root, env=env).returncode)
'''


def create_snapshot():
    if TARGET.exists() or ARCHIVE.exists():
        raise ValueError("snapshot_exists_no_overwrite")
    plan = json.loads(PLAN.read_text())
    data = {}
    current_candidates = {str(p): (d, k) for p, d, k in candidates()}
    for item in plan["exact_file_allowlist"]:
        if current_candidates.get(item["source"]) != (item["path"], item["kind"]):
            raise ValueError("allowlist_invalid")
        raw, reason = read_and_scan(Path(item["source"]), item["kind"])
        if reason or digest(raw) != item["sha256"]:
            raise ValueError("source_changed_refresh_plan")
        data[item["path"]] = raw
    required_paths = {
        "hamle/work/hamle-gmail-connector/.send-test-attempts/GC-HAMLE-GMAIL-20261009-01.attempt.json",
        "hamle/work/hamle-gmail-connector/.send-test-attempts/GC-HAMLE-GMAIL-20261009-01.accepted.json",
        "hamle/outputs/new-mac-2026-10-10/followup-completed-acceptance.json",
        "hamle/outputs/new-mac-2026-10-10/portable-business-state.json",
        "hamle/outputs/new-mac-2026-10-10/portable-business-state-post-eval.json",
        *("original-handover/" + name for name in ORIGINAL_NAMES),
    }
    if not required_paths.issubset(data):
        raise ValueError("required_handover_or_dedup_record_missing")
    current_state_path = "hamle/outputs/new-mac-2026-10-10/portable-business-state-post-eval.json"
    state_present = current_state_path in data
    status = {
        "schema_version": 1, "snapshot_created_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot_only": True, "live_system_modified": False,
        "provider_calls_during_export": 0, "credentials_included": False,
        "business_state_snapshot_included": state_present,
        "preferred_business_state_snapshot": current_state_path,
        "superseded_business_state_snapshot": "hamle/outputs/new-mac-2026-10-10/portable-business-state.json",
        "business_state_restore_tested": False,
        "legacy_handover_documents_are_historical": True,
        "canonical_recent_evidence": "hamle/outputs/new-mac-2026-10-10/",
        "current_preparation_status_file": "hamle/outputs/new-mac-2026-10-10/hybrid-preparation-current-status.json",
        "full_conversation_export_included": False,
        "original_progress_denominator": {"tasks": 65, "weights": 100},
        "latest_weighted_progress": {"completed_items": 21, "completed_points": 30},
        "live_customer_autonomy_accepted": False,
        "alternative_provider_adapter_live_accepted": False,
        "current_model_choice": "Sol + Astra preparation explicitly authorized; existing deployed V7 remains Astra/max. No live production profile changed.",
        "sol_evaluation": {"model": "gpt-6.1-sol", "effort": "medium", "live_synthetic_calls": 5, "new_astra_calls": 0, "routing_checks_passed": 5, "independent_ai_semantic_score": "39/40", "human_review_complete": False, "customer_autonomy_accepted": False, "token_cost_estimate_usd": 0.0434176, "cost_is_invoice": False, "conservative_reservation_retained_usd": 1},
        "local_preparation_tests_passed": 319,
        "pending_user_information": "Brand Basecamp projects, authorized customer emails, services, revision limits and approved assets; user will provide later.",
        "ozge_user_supplied_email": "ozge@gamechangers.com.tr",
        "ozge_user_supplied_basecamp_display_name": "Özge Yağmur",
        "ozge_basecamp_identity_id_verified": False,
        "spend_authorization": "Shared 50 USD until 2026-10-12T23:59:59+03:00; latest bounded comparison authorization is at most 1 USD within remaining total. No ongoing weekly limit approved.",
        "known_gaps": ["No full raw mail archive or all attachments", "Unseen CC removal remains an explicit authority boundary", "Brand/service customer scopes incomplete", "New provider-specific credentials/adapter/evals required for Claude or another service", "Production migration requires latest quiescent consistent state and read-only reconciliation; no restore automation acceptance"]
    }
    data.update({"README_TR.txt": README.encode(), "RESTORE_AND_PROVIDER_CHANGE_TR.txt": RESTORE.encode(),
                 "verify_manifest.py": VERIFY.encode(), "run_offline_tests.py": TESTS.encode(),
                 "HANDOVER_STATUS.json": (json.dumps(status, ensure_ascii=False, indent=2) + "\n").encode(),
                 "OMITTED_FILES.json": (json.dumps({"excluded_categories": plan["excluded_categories"], "omitted_for_review": plan["omitted_for_review"]}, ensure_ascii=False, indent=2) + "\n").encode()})
    entries = [{"path": name, "bytes": len(raw), "sha256": digest(raw)} for name, raw in sorted(data.items())]
    manifest = {"schema_version": 1, "snapshot_created_at": status["snapshot_created_at"],
                "private_company_material": True, "credentials_included": False,
                "original_sources_modified": False, "files": entries,
                "secret_scan": {"text_files": True, "json_credential_fields": True, "original_document_xml": True, "screenshots_and_binary_attachments_excluded": True, "patterns": [label for label, _ in SECRET_PATTERNS], "limitation": "Pattern scan is not proof against every possible secret format."},
                "counts_by_top_level": dict(Counter(name.split('/')[0] for name in data)),
                "business_state_snapshot_included": state_present}
    data["MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    TARGET.mkdir(mode=0o700)
    for name, raw in sorted(data.items()):
        private_write(TARGET / name, raw)
    with ARCHIVE.open("xb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, raw in sorted(data.items()):
                member = zipfile.ZipInfo(NAME + "/" + name)
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | 0o600) << 16
                member.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(member, raw)
    ARCHIVE.chmod(0o600)
    result = {"status": "created", "directory": str(TARGET), "archive": str(ARCHIVE),
              "archive_bytes": ARCHIVE.stat().st_size, "archive_sha256": digest(ARCHIVE.read_bytes()),
              "file_count": len(data), "business_state_snapshot_included": state_present,
              "omitted_for_review_count": len(plan["omitted_for_review"]), "network_calls": 0,
              "live_system_modified": False}
    private_write(ROOT / "outputs" / (NAME + "-receipt.json"), (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--create", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(prepare_plan() if args.plan else create_snapshot(), ensure_ascii=False, indent=2))
    except Exception as error:
        # Never print caught provider/file content or credential-shaped values.
        print(json.dumps({"status": "held", "error_type": type(error).__name__, "next_step": "Review allowlist and snapshot existence; no live service was changed."}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
