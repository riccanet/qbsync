import pytest

from qbsync.config import load_config
from qbsync.mock import MockQuickBooks
from qbsync.service import SyncService
from qbsync.store import Store


@pytest.fixture
def qb():
    return MockQuickBooks()


@pytest.fixture
def service(tmp_path, qb):
    cfg = load_config(None)
    cfg.update(connection="mock", database=str(tmp_path / "qb.db"), company_id="testco")
    cfg["sync"]["page_size"] = 10
    cfg["sync"]["overlap_seconds"] = 60
    svc = SyncService(cfg, Store(cfg["database"]))
    svc.make_connection = lambda: qb
    return svc
