"""Explicitly gated Cloud Run Job entrypoint. No live customer workflows."""
import argparse
from datetime import datetime, timezone
import json
import os
import uuid

from core import Config, Refused, authorize_network, initialize, run_once, set_enabled


def main(argv=None, env=None, store_factory=None, now=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("initialize", "arm", "stop", "run"))
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    config = Config(env.get("HAMLE_PROJECT_ID", ""), env.get("HAMLE_PROBE_NAMESPACE", ""),
                    env.get("HAMLE_FIRESTORE_DATABASE", ""), env.get("HAMLE_PROBE_RUN_ID", ""))
    store = None
    try:
        authorize_network(config, env.get("HAMLE_ALLOW_FIRESTORE_PROBE", ""),
                          now or datetime.now(timezone.utc))
        if store_factory is None:
            from firestore_store import FirestoreStore
            store_factory = lambda cfg: FirestoreStore(
                cfg, opt_in=env.get("HAMLE_ALLOW_FIRESTORE_PROBE", ""))
        store = store_factory(config)
        if args.operation == "initialize":
            outcome = initialize(store)
        elif args.operation in ("arm", "stop"):
            outcome = set_enabled(store, args.operation == "arm")
        else:
            outcome = run_once(store, config, uuid.uuid4().hex)
        print(json.dumps({"synthetic_only": True, "outcome": outcome}))
        return 0 if outcome in ("initialized_stopped", "already_initialized", "armed",
                                "stopped", "completed", "duplicate_completed") else 3
    except Refused as exc:
        print(json.dumps({"synthetic_only": True, "outcome": "refused", "reason": str(exc)}))
        return 2
    except Exception:
        # SDK errors may carry endpoints or environment details. Never log raw errors.
        print(json.dumps({"synthetic_only": True, "outcome": "unknown_hold",
                          "reason": "operation_failed_do_not_retry_automatically"}))
        return 4
    finally:
        if store is not None and hasattr(store, "close"):
            try:
                store.close()
            except Exception:
                pass  # Shutdown failure must not leak raw SDK/environment details.


if __name__ == "__main__":
    raise SystemExit(main())
