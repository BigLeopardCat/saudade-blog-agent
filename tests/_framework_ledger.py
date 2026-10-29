"""Share a real SQLite execution ledger while each worker owns its connection."""

from contextlib import closing

from agent import idem


class Ledger:
    def __init__(self, path):
        self.path = path

    def claim(self, *args, **kwargs):
        with closing(idem.Ledger(self.path)) as ledger:
            return ledger.claim(*args, **kwargs)

    def finish(self, *args, **kwargs):
        with closing(idem.Ledger(self.path)) as ledger:
            return ledger.finish(*args, **kwargs)

    def lookup(self, *args, **kwargs):
        with closing(idem.Ledger(self.path)) as ledger:
            return ledger.lookup(*args, **kwargs)

    def close(self):
        pass
