"""Firestore Native transaction adapter; only imported after explicit opt-in."""
from datetime import datetime, timezone

from core import Config, authorize_network


class FirestoreTx:
    def __init__(self, collection, transaction):
        self.collection = collection
        self.transaction = transaction

    def get(self, key):
        snapshot = self.collection.document(key).get(
            transaction=self.transaction, retry=None, timeout=10)
        return snapshot.to_dict() if snapshot.exists else None

    def create(self, key, data):
        self.transaction.create(self.collection.document(key), data)

    def put(self, key, data):
        self.transaction.set(self.collection.document(key), data)


class FirestoreStore:
    def __init__(self, config: Config, *, opt_in: str, clock=None):
        self.config = config
        self.opt_in = opt_in
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._authorize()
        from google.cloud import firestore
        self.sdk = firestore
        self.client = self.sdk.Client(project=config.project, database=config.database)
        self.collection = self.client.collection(config.namespace)

    def _authorize(self):
        # Read the current clock each time; never retain the entrypoint timestamp.
        authorize_network(self.config, self.opt_in, self.clock())

    def transact(self, callback):
        self._authorize()
        # Do not replay the application transaction after contention/errors.
        # An ambiguous commit stops this invocation; the stable run ID remains held.
        @self.sdk.transactional
        def apply(transaction):
            return callback(FirestoreTx(self.collection, transaction))

        return apply(self.client.transaction(max_attempts=1))

    def close(self):
        self.client.close()
