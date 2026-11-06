"""Share a real SQLite execution ledger while each worker owns its connection."""

from contextlib import closing

from eval.fixtures.ledger import Ledger as SQLiteLedger


class Ledger:
    def __init__(self, path):
        self.path = path

    def claim(self, *args, **kwargs):
        with closing(SQLiteLedger(self.path)) as ledger:
            return ledger.claim(*args, **kwargs)

    def finish(self, *args, **kwargs):
        with closing(SQLiteLedger(self.path)) as ledger:
            return ledger.finish(*args, **kwargs)

    def lookup(self, *args, **kwargs):
        with closing(SQLiteLedger(self.path)) as ledger:
            return ledger.lookup(*args, **kwargs)

    def replace(self, *args, **kwargs):
        with closing(SQLiteLedger(self.path)) as ledger:
            return ledger.replace(*args, **kwargs)

    def close(self):
        pass
