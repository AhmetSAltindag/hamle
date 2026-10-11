#!/usr/bin/env python3
"""Run the official local gcloud with a private project-specific configuration."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
config = ROOT / "private-connections" / "cloud-config"
config.mkdir(mode=0o700, parents=True, exist_ok=True)
config.chmod(0o700)
os.umask(0o077)
env = dict(os.environ)
env.update(CLOUDSDK_PYTHON=sys.executable, CLOUDSDK_CONFIG=str(config),
           CLOUDSDK_CORE_DISABLE_USAGE_REPORTING="true",
           CLOUDSDK_CORE_PROJECT="gamechangers-hamle",
           CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK="true")
binary = ROOT / "cloud-tools" / "google-cloud-sdk" / "bin" / "gcloud"
os.execve(str(binary), [str(binary), *sys.argv[1:]], env)
