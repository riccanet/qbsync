"""RequestProcessorConnection against a fake QBXMLRP2 COM object (the real one only exists on Windows)."""
import sys
import types

import pytest

from qbsync.connection import QBConnectionError, RequestProcessorConnection


class ComError(Exception):
    def __init__(self, hresult):
        super().__init__(hresult)
        self.hresult = hresult - (1 << 32)  # pywin32 reports HRESULTs as signed ints
        self.excepinfo = None


class FakeRP:
    def __init__(self, open_in_ui=None):
        self.open_in_ui = open_in_ui  # company file a user has open in QuickBooks on this PC
        self.ended = []

    def OpenConnection2(self, app_id, name, ctype):
        pass

    def BeginSession(self, path, mode):
        if self.open_in_ui and path and path != self.open_in_ui:
            raise ComError(0x8004040A)
        self.session_file = path or self.open_in_ui
        return "ticket:" + (path or "current")

    def GetCurrentCompanyFileName(self, ticket):
        return self.session_file

    def QBXMLVersionsForSession(self, ticket):
        return ("13.0", "16.0", "17.0")

    def EndSession(self, ticket):
        self.ended.append(ticket)

    def CloseConnection(self):
        pass


@pytest.fixture
def fake_com(monkeypatch):
    state = {}
    pythoncom = types.SimpleNamespace(com_error=ComError, CoInitialize=lambda: None)
    client = types.SimpleNamespace(Dispatch=lambda progid: state["rp"])
    win32com = types.ModuleType("win32com")
    win32com.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return state


UNC = r"\\filesrv01\qbdata\RiccaNET LLC\RiccaNET LLC.QBW"


def test_opens_configured_file(fake_com):
    fake_com["rp"] = FakeRP()
    conn = RequestProcessorConnection(UNC)
    conn.open()
    assert conn.open_file == UNC and conn.qbxml_version == "16.0"
    conn.close()


def test_uses_same_file_already_open_through_mapped_drive(fake_com):
    fake_com["rp"] = FakeRP(open_in_ui=r"Q:\RiccaNET LLC\RiccaNET LLC.QBW")
    conn = RequestProcessorConnection(UNC)
    conn.open()
    assert conn.open_file == r"Q:\RiccaNET LLC\RiccaNET LLC.QBW"
    conn.close()


def test_refuses_a_different_company_file(fake_com):
    rp = FakeRP(open_in_ui=r"Q:\Clients\Other Company.QBW")
    fake_com["rp"] = rp
    conn = RequestProcessorConnection(UNC)
    with pytest.raises(QBConnectionError, match="different company file open"):
        conn.open()
    assert rp.ended == ["ticket:current"]  # the probe session was closed


class StuckRP(FakeRP):
    """BeginSession launches a hidden QuickBooks that then never exits (the reported bug)."""

    def __init__(self, procs):
        super().__init__()
        self.procs = procs

    def BeginSession(self, path, mode):
        self.procs.add(4242)
        raise ComError(0x80040408)


@pytest.fixture
def fake_procs(monkeypatch):
    import qbsync.connection as c

    procs = {1000}  # a QuickBooks a user already had open: must survive
    killed = []
    monkeypatch.setattr(c, "_qb_pids", lambda: set(procs))
    monkeypatch.setattr(c, "_kill_pid", lambda pid: (killed.append(pid), procs.discard(pid)))
    clock = [0.0]
    monkeypatch.setattr(c.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(c.time, "monotonic", lambda: clock[0])
    return procs, killed


def test_failed_start_ends_the_quickbooks_it_launched(fake_com, fake_procs):
    procs, killed = fake_procs
    fake_com["rp"] = StuckRP(procs)
    with pytest.raises(QBConnectionError, match="was closed again"):
        RequestProcessorConnection(UNC).open()
    assert killed == [4242] and procs == {1000}


def test_failed_start_leaves_existing_quickbooks_alone(fake_com, fake_procs):
    procs, killed = fake_procs
    rp = FakeRP(open_in_ui=r"Q:\Clients\Other Company.QBW")
    fake_com["rp"] = rp
    with pytest.raises(QBConnectionError, match="different company file open"):
        RequestProcessorConnection(UNC).open()
    assert killed == [] and procs == {1000}


def test_successful_open_kills_nothing(fake_com, fake_procs):
    procs, killed = fake_procs
    fake_com["rp"] = FakeRP()
    conn = RequestProcessorConnection(UNC)
    conn.open()
    conn.close()
    assert killed == []
