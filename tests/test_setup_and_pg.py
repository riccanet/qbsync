import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from qbsync.config import load_config
from qbsync.setup_cmds import task_xml, write_config
from qbsync.sinks import PostgresSink


def test_init_writes_valid_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    assert write_config(cfg_path, r"\\QBSRV\QB Data\O'Brien & Co.QBW", webhook_url="https://n8n.example.com/webhook/qb")
    cfg = load_config(str(cfg_path))
    assert cfg["company_file"] == r"\\QBSRV\QB Data\O'Brien & Co.QBW"
    assert cfg["company_id"] == "O'Brien & Co"
    assert cfg["database"].endswith("O_Brien___Co.db")
    assert "api" not in cfg
    assert cfg["sinks"]["webhook"]["url"] == "https://n8n.example.com/webhook/qb"
    assert len(cfg["sinks"]["webhook"]["secret"]) > 30  # generated
    assert cfg["connection"] == "qbxmlrp2"
    # never clobbers an existing config
    assert write_config(cfg_path, "other.QBW") is False


def test_task_xml_is_well_formed():
    for at_boot in (False, True):
        xml = task_xml(Path(r"C:\ProgramData\QBSync\config.yaml"), r"CORP\svc & qb", at_boot)
        root = ET.fromstring(xml.split("\n", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        assert root.find("t:Principals/t:Principal/t:UserId", ns).text == r"CORP\svc & qb"
        args = root.find("t:Actions/t:Exec/t:Arguments", ns).text
        assert args.endswith('config.yaml" run') and args.startswith("-m qbsync -c")
        assert root.find("t:Triggers/t:BootTrigger" if at_boot else "t:Triggers/t:LogonTrigger", ns) is not None


PG_DSN = os.environ.get("QBSYNC_TEST_PG")


@pytest.mark.skipif(not PG_DSN, reason="set QBSYNC_TEST_PG to a postgres URL")
def test_postgres_sink(service, qb):
    import pg8000.dbapi

    sink = PostgresSink("testco", PG_DSN, table="qb_records_test", reports_table="qb_reports_test")
    conn = sink._connect()
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS qb_records_test, qb_reports_test")
    conn.commit()
    conn.close()
    service.sinks = [sink]
    service.run_once()
    qb.tick(120)
    inv = service.store.query("Invoice", limit=1)["records"][0]
    qb.modify("Invoice", inv["id"], Memo="pg update")
    service.run_once()

    conn = sink._connect()
    cur = conn.cursor()
    cur.execute("SELECT entity, count(*) FROM qb_records_test GROUP BY entity")
    counts = dict(cur.fetchall())
    assert counts["Invoice"] == 60 and counts["Customer"] == 37
    cur.execute("SELECT data->>'Memo', txn_date::text, amount FROM qb_records_test WHERE id=%s", (inv["id"],))
    memo, txn_date, amount = cur.fetchone()
    assert memo == "pg update" and txn_date == inv["txn_date"] and float(amount) == inv["amount"]
    cur.execute("SELECT data->>'title' FROM qb_reports_test WHERE name='profit_and_loss_ytd'")
    assert cur.fetchone()[0] == "Profit & Loss"
    cur.execute("SELECT relrowsecurity FROM pg_class WHERE relname='qb_records_test'")
    assert cur.fetchone()[0] is True
    conn.close()


def test_runs_without_std_streams(tmp_path):
    """pythonw.exe under Task Scheduler has no stdout/stderr; the CLI must still work and log to file."""
    import subprocess
    import sys

    cfg = tmp_path / "config.yaml"
    write_config(cfg, "", data_dir=tmp_path, connection="mock")
    code = (
        "import sys; sys.stdout = None; sys.stderr = None\n"
        "from qbsync.cli import main\n"
        f"rc = main(['-c', {str(cfg)!r}, 'sync', '--no-push'])\n"
        f"open({str(tmp_path / 'rc.txt')!r}, 'w').write(str(rc))\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parent.parent, timeout=120)
    assert (tmp_path / "rc.txt").read_text() == "0"
    log = next((tmp_path / "logs").glob("*.log")).read_text()
    assert "Sync ok" in log
