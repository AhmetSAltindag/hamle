"""One metadata-only GET for the explicitly selected model. No inference."""
import datetime
import json
from pathlib import Path
import stat
import requests

ROOT = Path(__file__).resolve().parents[1]
secret = ROOT / ".env.local"
if secret.is_symlink() or stat.S_IMODE(secret.stat().st_mode) != 0o600:
    raise SystemExit("private_file_required")
values = [line.split("=", 1)[1].strip().strip("\"'")
          for line in secret.read_text().splitlines()
          if line.startswith("OPENAI_API_KEY=")]
if len(values) != 1 or not values[0]:
    raise SystemExit("key_not_configured")
report = {
    "checked_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).isoformat(),
    "requested_model": "gpt-6-astra", "method": "GET",
    "endpoint": "https://api.openai.com/v1/models/gpt-6-astra",
    "inference_calls": 0, "customer_data_sent": False,
    "reasoning_max_live_verified": False, "billing_verified": False,
}
session = requests.Session()
session.trust_env = False
try:
    response = session.get(report["endpoint"],
        headers={"Authorization": "Bearer " + values[0]},
        timeout=30, allow_redirects=False)
    report["http_status"] = response.status_code
    if response.status_code == 200:
        data = response.json()
        report["metadata_access_verified"] = data.get("id") == "gpt-6-astra"
        report["model_id"] = data.get("id")
        report["owned_by"] = data.get("owned_by")
    else:
        report["metadata_access_verified"] = False
        report["error"] = "metadata_request_rejected"
except requests.RequestException:
    report["metadata_access_verified"] = False
    report["error"] = "network_or_tls_error"
finally:
    session.close()
(ROOT / "outputs/new-mac-2026-10-10/openai-metadata-verification.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
