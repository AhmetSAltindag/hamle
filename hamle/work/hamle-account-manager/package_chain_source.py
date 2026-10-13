"""Credential-free source package for the v1.1 internal chain image; never builds or uploads.

Same rules as package_cloud_source.py (explicit allowlist, no recursion, secret scan,
compile check, no overwrite). Adds the chain modules, prompts and the brand profile +
quality rubric as work/hamle-account-manager/chain-profile/ (the loaders read there
first). The profile/rubric are copied AS THEY ARE ON DISK: while the catalog is a draft
(live_enabled=false) or the rubric is draft_not_accepted, the deployed chain holds.
"""
from __future__ import annotations

import argparse
import json

import package_cloud_source as base

APP = base.APP
PACKAGE_NAME = "app-source-20261012-v11-chain"
PROFILE = "outputs/gamechangers-internal/"
CHAIN_FILES = ("v11_chain.py", "v11_brand_task.py", "v11_intake_plan.py", "v11_basecamp_reader.py",
               "v11_poll_journal.py", "v11_chain_models.py", "v11_chain_mail.py", "v11_chain_quality.py",
               "v11_chain_live.py", "v11_chain_authority.py", "v11_daily_digest.py", "chain_interpreter_prompt.txt", "chain_classifier_prompt.txt",
               "chain_answerer_prompt.txt", "chain_digest_prompt.txt",
               "v11_trend_search.py", "chain_trend_prompt.txt", "model_routing.py",
               "chain_special_days_prompt.txt", "chain_new_business_prompt.txt", "v11_brand_routing.py",
               "v11_client_watch.py", "v11_client_reply.py", "client_draft_prompt.txt",
               "chain_daily_spark_prompt.txt", "v11_nb_tracking.py",
               "v11_idea_tracking.py", "v11_client_job.py", "client_job_review_prompt.txt")
ALLOWLIST = dict(base.ALLOWLIST)
ALLOWLIST["cloudbuild.yaml"] = APP + "cloudbuild-v11-chain.yaml"
ALLOWLIST.update({APP + name: APP + name for name in CHAIN_FILES})
ALLOWLIST.update({APP + "chain-profile/" + name: PROFILE + name for name in (
    "gamechangers-internal-catalog.json", "gamechangers-internal-quality-rubric.json",
    "gamechangers-internal-work-authority-policy.json", "daily-digest-config.json",
    "brand-cards.json", "tr-special-days-2026.json", "model-routing.json", "client-watch-config.json")})


def package(root=base.ROOT):
    saved = base.ALLOWLIST, base.PACKAGE_NAME
    base.ALLOWLIST, base.PACKAGE_NAME = ALLOWLIST, PACKAGE_NAME
    try:
        return base.package(root)
    finally:
        base.ALLOWLIST, base.PACKAGE_NAME = saved


def collect(root=base.ROOT):
    saved = base.ALLOWLIST, base.PACKAGE_NAME
    base.ALLOWLIST, base.PACKAGE_NAME = ALLOWLIST, PACKAGE_NAME
    try:
        return base.collect(root)
    finally:
        base.ALLOWLIST, base.PACKAGE_NAME = saved


def main():
    parser = argparse.ArgumentParser(description="Create only the allowlisted v1.1 chain Cloud source")
    parser.add_argument("--create", action="store_true", required=True)
    parser.parse_args()
    try:
        print(json.dumps(package(), ensure_ascii=False))
    except Exception:
        print(json.dumps({"status": "held", "reason": "source_package_failed_review_required"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
