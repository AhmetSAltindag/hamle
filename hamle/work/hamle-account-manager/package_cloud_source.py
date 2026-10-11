"""Create an explicit, credential-free source folder/tar; never build or upload."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import tarfile

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "app-source-20261010-v10"
APP = "work/hamle-account-manager/"
# Every included file is named here. No directory recursion or environment files.
ALLOWLIST = {
    "Dockerfile": APP + "Dockerfile",
    "requirements-cloud.txt": APP + "requirements-cloud.txt",
    "cloudbuild.yaml": APP + "cloudbuild.yaml",
    APP + "cloud_control.py": APP + "cloud_control.py",
    APP + "cloud_ui.html": APP + "cloud_ui.html",
    APP + "cloud_store.py": APP + "cloud_store.py",
    APP + "runtime_authority.py": APP + "runtime_authority.py",
    APP + "cloud_providers.py": APP + "cloud_providers.py",
    APP + "basecamp_cloud_auth.py": APP + "basecamp_cloud_auth.py",
    APP + "worker.py": APP + "worker.py",
    APP + "dispatch.py": APP + "dispatch.py",
    APP + "recipient_policy.py": APP + "recipient_policy.py",
    APP + "business_writers.py": APP + "business_writers.py",
    APP + "model.py": APP + "model.py",
    APP + "model_profiles.py": APP + "model_profiles.py",
    APP + "account_manager_prompt.txt": APP + "account_manager_prompt.txt",
    APP + "autonomy_policy.py": APP + "autonomy_policy.py",
    APP + "v11_quality_gate.py": APP + "v11_quality_gate.py",
    APP + "v11_delivery.py": APP + "v11_delivery.py",
    APP + "v11_production_stages.py": APP + "v11_production_stages.py",
    APP + "v11_artifact_inspector.py": APP + "v11_artifact_inspector.py",
    APP + "v11_internal_providers.py": APP + "v11_internal_providers.py",
    APP + "v11_internal_state.py": APP + "v11_internal_state.py",
    APP + "v11_internal_quality.py": APP + "v11_internal_quality.py",
    APP + "v11_internal_worker.py": APP + "v11_internal_worker.py",
    APP + "v11_work_authority.py": APP + "v11_work_authority.py",
    APP + "v11_work_authority_store.py": APP + "v11_work_authority_store.py",
    APP + "v11_work_queue.py": APP + "v11_work_queue.py",
    APP + "v11_stop_intake.py": APP + "v11_stop_intake.py",
    APP + "v11_wat_media_inspector.py": APP + "v11_wat_media_inspector.py",
    APP + "v11_wat_readonly.py": APP + "v11_wat_readonly.py",
    APP + "v11_wat_vision.py": APP + "v11_wat_vision.py",
    APP + "v11_wat_shadow_worker.py": APP + "v11_wat_shadow_worker.py",
    APP + "wat_media_smoke.py": APP + "wat_media_smoke.py",
    APP + "v11_wat_video_shadow.py": APP + "v11_wat_video_shadow.py",
    APP + "v11_wat_timed_frame.py": APP + "v11_wat_timed_frame.py",
    APP + "wat-video-shadow-cases.json": APP + "wat-video-shadow-cases.json",
    APP + "wat-shadow-cases.json": APP + "wat-shadow-cases.json",
    APP + "wat-reference/detail-03.png": APP + "wat-reference/detail-03.png",
    APP + "wat-reference/detail-08.png": APP + "wat-reference/detail-08.png",
    APP + "wat-reference/detail-09.png": APP + "wat-reference/detail-09.png",
    APP + "internal_ocr_smoke.py": APP + "internal_ocr_smoke.py",
    "work/hamle-gmail-connector/hamle_gmail.py": "work/hamle-gmail-connector/hamle_gmail.py",
    "work/hamle-workflow/gmail_full_reader.py": "work/hamle-workflow/gmail_full_reader.py",
    "work/hamle-workflow/mail_ingest.py": "work/hamle-workflow/mail_ingest.py",
    "work/hamle-internal-intake/intake.py": "work/hamle-internal-intake/intake.py",
}
SECRET_PATTERNS = (
    re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{30,}"),
    re.compile(rb"GOCSPX-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
)


def collect(root=ROOT):
    root = root.resolve()
    data = {}
    for destination, source_name in ALLOWLIST.items():
        source = root / source_name
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError("source_symlink_or_escape")
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            raise ValueError("source_not_bounded_regular_file")
        raw = source.read_bytes()
        if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
            raise ValueError("possible_embedded_credential_requires_review")
        if destination.endswith(".py"):
            compile(raw, destination, "exec")
        data[destination] = raw
    manifest = {"schema_version": 1, "package": PACKAGE_NAME,
                "source_allowlist_count": len(data), "credentials_included": False,
                "files": [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in sorted(data.items())]}
    data["SOURCE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    return data


def package(root=ROOT):
    data = collect(root)
    destination = root / "outputs" / PACKAGE_NAME
    archive = root / "outputs" / (PACKAGE_NAME + ".tar.gz")
    if destination.exists() or archive.exists():
        raise ValueError("package_exists_no_overwrite")
    destination.mkdir(mode=0o755, parents=True)
    for name, raw in sorted(data.items()):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
        target.chmod(0o644)
    # Tar members are exactly the allowlist + generated manifest, never parent
    # directory contents. No filesystem metadata, symlinks or user paths leak.
    with archive.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            for name, raw in sorted(data.items()):
                member = tarfile.TarInfo(name)
                member.size = len(raw); member.mode = 0o644
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ""
                tar.addfile(member, io.BytesIO(raw))
    archive.chmod(0o644)
    return {"status": "packaged", "file_count": len(data), "directory": str(destination),
            "archive": str(archive), "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "network_calls": 0, "build_started": False, "upload_started": False}


def main():
    parser = argparse.ArgumentParser(description="Create only Hamle allowlisted Cloud source")
    parser.add_argument("--create", action="store_true", required=True)
    parser.parse_args()
    try:
        print(json.dumps(package(), ensure_ascii=False))
    except Exception:
        # No file content, token or accidental provider exceptions are printed.
        print(json.dumps({"status": "held", "reason": "source_package_failed_review_required"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
