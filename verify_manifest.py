#!/usr/bin/env python3
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
