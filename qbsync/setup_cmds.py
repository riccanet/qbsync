"""``qbsync init`` (write a config file) and ``qbsync service`` (Windows Scheduled Task)."""
from __future__ import annotations

import getpass
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

TEMPLATE = Path(__file__).with_name("config.template.yaml")
DEFAULT_TASK = "QBSync"


def _yaml_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_config(
    path: Path,
    company_file: str,
    company_id: str | None = None,
    data_dir: Path | None = None,
    connection: str = "qbxmlrp2",
    force: bool = False,
    webhook_url: str = "",
    webhook_secret: str = "",
) -> bool:
    """Render the config template. Returns False if it exists (never overwrites unless forced).

    With a webhook URL but no secret, a secret is generated (read it back from the config).
    """
    if path.exists() and not force:
        return False
    company_file = company_file.strip().strip('"')
    company_id = company_id or (PureWindowsPath(company_file).stem if company_file else "company")
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in company_id) or "company"
    data_dir = data_dir or path.parent
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in {
        "COMPANY_FILE": _yaml_str(company_file),
        "COMPANY_ID": _yaml_str(company_id),
        "CONNECTION": connection,
        "DATABASE": _yaml_str(str(data_dir / "data" / f"{safe_id}.db")),
        "LOG_FILE": _yaml_str(str(data_dir / "logs" / f"{safe_id}.log")),
        "WEBHOOK": _webhook_yaml(webhook_url.strip().strip('"'), webhook_secret.strip().strip('"')),
    }.items():
        text = text.replace("{{" + key + "}}", value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _webhook_yaml(url: str, secret: str) -> str:
    if not url:
        return "null"
    secret = secret or secrets.token_urlsafe(32)
    return f"{{url: {_yaml_str(url)}, secret: {_yaml_str(secret)}, send_status: errors, batch_size: 200}}"


# ---- Windows Scheduled Task ---------------------------------------------------------


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def _launcher() -> str:
    """pythonw.exe (no console window) next to the running interpreter, if present."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe)


def task_xml(config: Path, user: str, at_boot: bool) -> str:
    trigger = (
        "<BootTrigger><Enabled>true</Enabled><Delay>PT1M</Delay></BootTrigger>"
        if at_boot
        else f"<LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>"
    )
    logon_type = "Password" if at_boot else "InteractiveToken"
    args = f'-m qbsync -c "{config}" run'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>QB Sync: read-only QuickBooks Desktop sync with push to webhooks / Postgres.</Description>
  </RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>{logon_type}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_launcher())}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(config.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> int:
    return subprocess.call(["schtasks", *args])


def service_install(config: Path, user: str | None, password: str | None, name: str = DEFAULT_TASK) -> int:
    """Without a password the task runs at the user's logon, in their session (most reliable for
    QuickBooks). With a password it starts at boot whether or not anyone is logged on."""
    user = user or current_user()
    xml = task_xml(config.resolve(), user, at_boot=bool(password))
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as fh:
        fh.write(xml)
        xml_path = fh.name
    try:
        cmd = ["/Create", "/TN", name, "/XML", xml_path, "/F"]
        if password:
            cmd += ["/RU", user, "/RP", password]
        rc = _schtasks(*cmd)
    finally:
        os.unlink(xml_path)
    if rc == 0:
        mode = "at boot" if password else f"when {user} logs on"
        print(f"Scheduled task '{name}' installed: runs {mode}.")
    return rc


def service_command(action: str, name: str = DEFAULT_TASK) -> int:
    if action == "uninstall":
        _schtasks("/End", "/TN", name)
        return _schtasks("/Delete", "/TN", name, "/F")
    if action == "start":
        return _schtasks("/Run", "/TN", name)
    if action == "stop":
        return _schtasks("/End", "/TN", name)
    if action == "status":
        return _schtasks("/Query", "/TN", name, "/V", "/FO", "LIST")
    raise ValueError(action)
