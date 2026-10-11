#!/usr/bin/env python3
"""Read only the dedicated synthetic Firestore collection, keeping auth private."""
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent
URL = ("https://firestore.googleapis.com/v1/projects/gamechangers-hamle/"
       "databases/hamle-synthetic/documents/hamle-synthetic-probe-v1?pageSize=20")


def main():
    result = subprocess.run([sys.executable, str(ROOT / "cloud_gcloud.py"),
                             "auth", "print-access-token",
                             "--account=ahmet@gamechangers.com.tr"],
                            capture_output=True, text=True, check=False)
    if result.returncode or not result.stdout.strip():
        print(json.dumps({"status": "auth_unavailable"}))
        return 2
    request = Request(URL, headers={"Authorization": "Bearer " + result.stdout.strip()})
    try:
        with urlopen(request, timeout=20) as response:
            data = json.load(response)
    except HTTPError as exc:
        print(json.dumps({"status": "read_failed", "http_status": exc.code}))
        return 3
    except Exception:
        print(json.dumps({"status": "read_failed"}))
        return 3
    if data.get("nextPageToken"):
        print(json.dumps({"status": "unexpected_pagination"}))
        return 4
    evidence = {"synthetic_only": True, "source": URL,
                "documents": data.get("documents", [])}
    destination = ROOT.parent / "outputs/new-mac-2026-10-10/cloud-probe-records.json"
    destination.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
