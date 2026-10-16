"""Connection to QuickBooks Desktop through the QuickBooks SDK request processor.

The company file hosted by QuickBooks Database Server Manager is an encrypted
SQL Anywhere database that cannot be queried directly. Supported access goes
through a QuickBooks Desktop install on the machine running this program; the
SDK's ``QBXMLRP2.RequestProcessor`` COM object talks to that install, which
opens the file on the server (e.g. ``\\\\QBSERVER\\QBData\\Company.QBW``).
"""
from __future__ import annotations

import csv
import logging
import os
import subprocess
import threading
import time
from pathlib import PureWindowsPath
from typing import Protocol

log = logging.getLogger(__name__)

CT_LOCAL_QBD = 1
# QBXMLRP2 AuthPreferences enums (QBXMLRPUnattendedModePrefType / QBXMLRPPersonalDataPrefType)
UNATTENDED_MODE = {"optional": 0, "required": 1}
PERSONAL_DATA = {"optional": 0, "required": 1, "not_needed": 2}
FILE_MODES = {"single_user": 0, "multi_user": 1, "dont_care": 2}
MAX_QBXML_VERSION = 16.0

# Common COM failures and what usually fixes them.
HRESULT_HINTS = {
    0x80040430: "QuickBooks has not approved automatic login for this app. With QuickBooks open as Admin: Edit > "
    "Preferences > Integrated Applications > Company Preferences, Remove every QB Sync entry, save, then run "
    "Test QuickBooks Connection and choose 'Yes, always; allow access even if QuickBooks is not running'. "
    "Also make sure company_file is exactly the path QuickBooks uses (a mapped drive like Q:\\ and a UNC path "
    "count as different files), and that the login user has signed in to QuickBooks once.",
    0x80040440: "QuickBooks only allows unattended access to a company file whose users have complex passwords. Set a "
    "complex password for Admin and for the QB Sync login user (Company > Set Up Users and Passwords), then log "
    "in to QuickBooks once as the QB Sync user, since QuickBooks may force a password change at first login.",
    0x80040408: "Could not start QuickBooks. Is QuickBooks Desktop installed on this machine and can this "
    "Windows user open it?",
    0x8004040A: "QuickBooks is open on this PC with a different company file (or the same file through a "
    "different path, e.g. a mapped drive). Close QuickBooks, or set company_file to the exact path QuickBooks "
    "shows under Help > Product Information (F2).",
    0x80040414: "A modal dialog is open in QuickBooks. Close it (or close QuickBooks) and retry.",
    0x80040416: "QuickBooks is not running, so company_file must be set to the full path of the .QBW file.",
    0x80040418: "This application has not accessed this company file before. Open the file in QuickBooks as "
    "Admin and approve the access prompt (see README: First-time authorization).",
    0x8004041A: "This application does not have permission. In QuickBooks: Edit > Preferences > Integrated "
    "Applications > Company Preferences, and allow the app.",
    0x80040420: "The QuickBooks user denied access to this application.",
    0x80040422: "The file must be opened in single-user mode, but another user has it in multi-user mode.",
    0x80040424: "QuickBooks did not finish starting. Retry, or start QuickBooks and log in once on this machine.",
}


class QBConnectionError(Exception):
    pass


class QBConnection(Protocol):
    qbxml_version: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def process(self, request_xml: str) -> str: ...


HR_FILE_ALREADY_OPEN = 0x8004040A
_com_thread = threading.local()
QB_PROCESS_NAMES = ("QBW.EXE", "QBW32.EXE")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flash under pythonw


def _qb_pids() -> set[int]:
    """PIDs of running QuickBooks Desktop processes (empty if tasklist is unavailable)."""
    pids: set[int] = set()
    for name in QB_PROCESS_NAMES:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for row in csv.reader(out.splitlines()):
            if len(row) > 1 and row[0].upper() == name and row[1].isdigit():
                pids.add(int(row[1]))
    return pids


def _kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20,
                       creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        log.debug("taskkill %s failed", pid, exc_info=True)


def end_quickbooks_started_since(before: set[int], grace_seconds: float = 10.0) -> list[int]:
    """End QuickBooks processes that were not running in ``before``.

    When the SDK starts QuickBooks in the background and the session then fails, that hidden
    QuickBooks often never exits and stops anyone from opening QuickBooks. Processes that were
    already running (a user's QuickBooks) are never touched.
    """
    deadline = time.monotonic() + grace_seconds
    started = _qb_pids() - before
    while started and time.monotonic() < deadline:
        time.sleep(1)
        started = _qb_pids() - before
    for pid in sorted(started):
        log.warning("Ending background QuickBooks process %d left behind by the failed connection", pid)
        _kill_pid(pid)
    return sorted(started)


def _hresult(exc: Exception) -> int | None:
    excepinfo = getattr(exc, "excepinfo", None)
    if excepinfo and len(excepinfo) > 5 and excepinfo[5]:
        return excepinfo[5] & 0xFFFFFFFF
    if getattr(exc, "hresult", None) is not None:
        return exc.hresult & 0xFFFFFFFF
    return None


def same_company_file(a: str, b: str) -> bool:
    """True when two paths name the same .QBW, e.g. a UNC path and a mapped-drive path to it."""
    pa, pb = PureWindowsPath(a.strip()), PureWindowsPath(b.strip())
    return bool(pa.name) and pa.name.lower() == pb.name.lower()


def _explain_com_error(exc: Exception) -> str:
    excepinfo = getattr(exc, "excepinfo", None)
    code = _hresult(exc)
    if code is None:
        return str(exc)
    hint = HRESULT_HINTS.get(code)
    detail = excepinfo[2] if excepinfo and len(excepinfo) > 2 and excepinfo[2] else str(exc)
    return f"0x{code:08X}: {detail}" + (f" -- {hint}" if hint else "")


class RequestProcessorConnection:
    """Talks to QuickBooks via the QBXMLRP2 COM object (Windows only)."""

    def __init__(
        self,
        company_file: str = "",
        app_name: str = "QB Sync",
        app_id: str = "",
        qbxml_version: str = "auto",
        file_mode: str = "dont_care",
        unattended_mode: str = "required",
        read_only: bool = True,
        personal_data: str = "not_needed",
    ):
        self.company_file = company_file
        self.app_name = app_name
        self.app_id = app_id
        self.requested_version = qbxml_version
        self.qbxml_version = qbxml_version if qbxml_version != "auto" else "13.0"
        self.file_mode = FILE_MODES[file_mode]
        self.unattended_mode = UNATTENDED_MODE[unattended_mode]
        self.read_only = bool(read_only)
        self.personal_data = PERSONAL_DATA[personal_data]
        self._rp = None
        self._ticket = None
        self.open_file: str | None = None  # the company file QuickBooks actually opened

    def open(self) -> None:
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise QBConnectionError(
                "pywin32 is not installed. This connection only works on Windows: pip install pywin32"
            ) from exc
        if not getattr(_com_thread, "initialized", False):
            # Once per thread and never uninitialized: releasing COM objects after CoUninitialize
            # produces "Win32 exception occurred releasing IUnknown" noise.
            pythoncom.CoInitialize()
            _com_thread.initialized = True
        qb_before = _qb_pids()
        try:
            try:
                self._rp = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
            except pythoncom.com_error as exc:
                raise QBConnectionError(
                    "Could not create QBXMLRP2.RequestProcessor. Install the QuickBooks SDK / QuickBooks Desktop "
                    "on this machine. 'Class not registered' usually means a 32/64-bit mismatch between Python "
                    f"and the installed SDK components. ({_explain_com_error(exc)})"
                ) from exc
            self._rp.OpenConnection2(self.app_id, self.app_name, CT_LOCAL_QBD)
            self._set_auth_preferences()
            try:
                self._ticket = self._rp.BeginSession(self.company_file, self.file_mode)
            except pythoncom.com_error as exc:
                if _hresult(exc) != HR_FILE_ALREADY_OPEN or not self.company_file:
                    raise
                self._ticket = self._use_already_open_file(exc)
            try:
                self.open_file = self._rp.GetCurrentCompanyFileName(self._ticket) or self.company_file
            except Exception:
                self.open_file = self.company_file
            if self.requested_version == "auto":
                self.qbxml_version = self._detect_version()
            log.info("Connected to QuickBooks (qbXML %s)", self.qbxml_version)
        except QBConnectionError as exc:
            self.close()
            self._cleanup_failed_start(qb_before, exc)
            raise
        except Exception as exc:
            self.close()
            error = QBConnectionError(f"Could not open QuickBooks session: {_explain_com_error(exc)}")
            self._cleanup_failed_start(qb_before, error)
            raise error from exc

    @staticmethod
    def _cleanup_failed_start(qb_before: set[int], error: Exception) -> None:
        ended = end_quickbooks_started_since(qb_before)
        if ended:
            error.args = (f"{error.args[0]} (QuickBooks had been started in the background for this attempt "
                          "and was closed again.)",)

    def _set_auth_preferences(self) -> None:
        """Tell QuickBooks what access to ask the administrator for (must precede BeginSession).

        Requiring unattended mode makes the approval prompt offer only "Yes, always; allow access
        even if QuickBooks is not running", so the grant actually covers automatic login when
        QuickBooks is closed. QB Sync only reads, so it asks for read-only access.
        """
        try:
            prefs = self._rp.AuthPreferences
            prefs.PutUnattendedModePref(self.unattended_mode)
            prefs.PutIsReadOnly(self.read_only)
            prefs.PutPersonalDataPref(self.personal_data)
        except Exception as exc:
            log.warning("Could not set QuickBooks authorization preferences: %s", _explain_com_error(exc))

    def _use_already_open_file(self, original: Exception) -> str:
        """QuickBooks is open on this PC (someone is working in it). If it has the same company file
        open, just through a different path such as a mapped drive, sync through that session.
        Never sync a different company file."""
        try:
            ticket = self._rp.BeginSession("", self.file_mode)
        except Exception:
            raise original
        try:
            open_file = self._rp.GetCurrentCompanyFileName(ticket)
        except Exception:
            open_file = ""
        if open_file and same_company_file(open_file, self.company_file):
            log.info("QuickBooks already has %s open; using that session", open_file)
            return ticket
        try:
            self._rp.EndSession(ticket)
        except Exception:
            pass
        raise QBConnectionError(
            f"QuickBooks on this PC has a different company file open ({open_file or 'unknown'}), "
            f"not {self.company_file}. QB Sync will not sync another company's data. Close that file in "
            "QuickBooks, or fix company_file in the config."
        )

    def _detect_version(self) -> str:
        try:
            versions = self._rp.QBXMLVersionsForSession(self._ticket)
        except Exception:
            return "13.0"
        usable = []
        for v in versions or ():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f <= MAX_QBXML_VERSION:
                usable.append((f, v))
        return max(usable)[1] if usable else "13.0"

    def process(self, request_xml: str) -> str:
        if self._rp is None or self._ticket is None:
            raise QBConnectionError("Connection is not open")
        try:
            return self._rp.ProcessRequest(self._ticket, request_xml)
        except Exception as exc:
            raise QBConnectionError(f"ProcessRequest failed: {_explain_com_error(exc)}") from exc

    def close(self) -> None:
        if self._rp is not None:
            if self._ticket is not None:
                try:
                    self._rp.EndSession(self._ticket)
                except Exception:
                    log.debug("EndSession failed", exc_info=True)
            try:
                self._rp.CloseConnection()
            except Exception:
                log.debug("CloseConnection failed", exc_info=True)
        self._rp = None
        self._ticket = None

    def __enter__(self) -> "RequestProcessorConnection":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
