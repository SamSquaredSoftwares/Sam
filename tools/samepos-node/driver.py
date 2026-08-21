#!/usr/bin/env python3
"""Headless driver for commissioning a SAMePOS venue node from a remote shell.

This implements the execution pattern the SAMePOS install runbook calls for, on a
machine that has no PowerShell, no SMB client and no remote desktop: launch each
install phase as a *detached* process on the target, poll its PID, then read the
phase log back over SMB. A detached process survives the orchestration call that
started it, so a 2-minute `configure-node.ps1` never trips a ~60s tool timeout.

Two execution channels, because the runbook needs both at different moments:

* ``cim``   -- DCOM/WMI ``Win32_Process.Create``. Works before WinRM is enabled,
              and with ``LocalAccountTokenFilterPolicy=1`` it hands back a *full*
              admin token, so phases run elevated. This is the preferred channel.
* ``winrm`` -- WS-Man. Convenient for short synchronous probes; also used to run
              a loopback ``Invoke-CimMethod`` when only 5985 is reachable.

File transfer is always SMB, and deliberately never relies on ``C$``: on a
token-filtered venue box the admin shares keep refusing even after the policy is
set, so payloads go to a writable data share and elevated processes on the target
write to ``C:`` themselves.

Nothing here mutates the target. Every subcommand either reads state or runs a
command you supply, so the destructive decisions stay in the runbook where they
can be verified phase by phase.

Usage
-----
Credentials come from the environment so they never land in shell history or a
process list::

    export SAMEPOS_HOST=<ip-or-host> SAMEPOS_USER='HOST\\account' SAMEPOS_PASS='...'

    python3 driver.py probe                       # prove creds, diagnose access
    python3 driver.py shares                      # what is writable for transfer
    python3 driver.py put phase0_recon.ps1 --share E --dest samepos\\phase0_recon.ps1
    python3 driver.py launch --script 'E:\\samepos\\phase0_recon.ps1' \\
        --log 'E:\\samepos_logs\\phase0.log'      # prints a PID, returns at once
    python3 driver.py poll <pid>                  # RUNNING | GONE
    python3 driver.py get --share E --path samepos_logs\\phase0.log
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------
# Access diagnosis vocabulary.
#
# The runbook hinges on one distinction: "Access is denied" on C$ while IPC$
# authenticates means the credentials are RIGHT and UAC remote-token filtering
# is the blocker -- a bad password reports a different NT status entirely. Get
# this wrong and you spend an hour re-checking a password that was never wrong.
# --------------------------------------------------------------------------

ACCESS_OK = "ok"
ACCESS_TOKEN_FILTERED = "token_filtered"
ACCESS_BAD_CREDENTIALS = "bad_credentials"
ACCESS_UNREACHABLE = "unreachable"
ACCESS_ERROR = "error"

_BAD_CRED_STATUSES = (
    "STATUS_LOGON_FAILURE",
    "STATUS_ACCOUNT_DISABLED",
    "STATUS_ACCOUNT_LOCKED_OUT",
    "STATUS_PASSWORD_EXPIRED",
    "STATUS_PASSWORD_MUST_CHANGE",
    "STATUS_WRONG_PASSWORD",
)

_DENIED_STATUSES = (
    "STATUS_ACCESS_DENIED",
    "rpc_s_access_denied",
    "E_ACCESSDENIED",
    "0x80070005",
)

# Shares that exist on every Windows box but tell us nothing about admin rights.
_UNINTERESTING_SHARES = {"IPC$", "print$"}


def classify_error(exc: BaseException) -> str:
    """Map a transport exception onto the runbook's access vocabulary."""
    text = str(exc)
    if any(status in text for status in _BAD_CRED_STATUSES):
        return ACCESS_BAD_CREDENTIALS
    if any(status in text for status in _DENIED_STATUSES):
        return ACCESS_TOKEN_FILTERED
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("timed out", "timeout", "connection refused", "unreachable", "no route")
    ):
        return ACCESS_UNREACHABLE
    return ACCESS_ERROR


# --------------------------------------------------------------------------
# Target definition
# --------------------------------------------------------------------------


@dataclass
class Target:
    """Where the venue box is and how to authenticate to it."""

    host: str
    user: str
    password: str
    domain: str = ""
    smb_port: int = 445
    winrm_port: int = 5985
    winrm_scheme: str = "http"

    @classmethod
    def from_env(cls, args: argparse.Namespace | None = None) -> "Target":
        """Build a target from the environment, with optional CLI overrides.

        The password is only ever read from the environment: passing it as an
        argument would expose it in the target's own process list once we start
        shelling out, and in this shell's history on the way there.
        """
        host = getattr(args, "host", None) or os.environ.get("SAMEPOS_HOST", "")
        user = getattr(args, "user", None) or os.environ.get("SAMEPOS_USER", "")
        password = os.environ.get("SAMEPOS_PASS", "")

        missing = [
            name
            for name, value in (
                ("SAMEPOS_HOST", host),
                ("SAMEPOS_USER", user),
                ("SAMEPOS_PASS", password),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "missing credentials: "
                + ", ".join(missing)
                + "\nSet them in the environment, e.g.\n"
                "  export SAMEPOS_HOST=203.0.113.10 "
                "SAMEPOS_USER='VENUEPC\\\\samadmin' SAMEPOS_PASS='...'"
            )

        # Accept HOST\user and user@domain as well as a bare account name.
        domain = ""
        if "\\" in user:
            domain, user = user.split("\\", 1)
        elif "@" in user:
            user, domain = user.split("@", 1)

        return cls(
            host=host,
            user=user,
            password=password,
            domain=domain,
            smb_port=int(getattr(args, "smb_port", None) or os.environ.get("SAMEPOS_SMB_PORT", 445)),
            winrm_port=int(
                getattr(args, "winrm_port", None) or os.environ.get("SAMEPOS_WINRM_PORT", 5985)
            ),
            winrm_scheme=getattr(args, "winrm_scheme", None)
            or os.environ.get("SAMEPOS_WINRM_SCHEME", "http"),
        )

    @property
    def winrm_endpoint(self) -> str:
        return f"{self.winrm_scheme}://{self.host}:{self.winrm_port}/wsman"


# --------------------------------------------------------------------------
# SMB: credential proof, share discovery, file transfer
# --------------------------------------------------------------------------


class SmbChannel:
    """Thin wrapper over impacket's SMBConnection for transfer and diagnosis."""

    def __init__(self, target: Target, timeout: int = 20) -> None:
        self.target = target
        self.timeout = timeout
        self._conn: Any = None

    def connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        from impacket.smbconnection import SMBConnection

        conn = SMBConnection(
            remoteName=self.target.host,
            remoteHost=self.target.host,
            sess_port=self.target.smb_port,
            timeout=self.timeout,
        )
        conn.login(self.target.user, self.target.password, self.target.domain)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def list_shares(self) -> list[str]:
        conn = self.connect()
        names = []
        for entry in conn.listShares():
            # impacket returns a null-terminated fixed field here.
            names.append(entry["shi1_netname"][:-1])
        return names

    def can_read(self, share: str, path: str = "\\*") -> tuple[bool, str]:
        """Try to list a share. Returns (ok, diagnosis)."""
        try:
            self.connect().listPath(share, path)
            return True, ACCESS_OK
        except Exception as exc:  # noqa: BLE001 - classifying, then re-reporting
            return False, classify_error(exc)

    def put(self, local_path: str, share: str, dest_path: str) -> int:
        """Upload a local file. Creates intermediate directories on the share."""
        conn = self.connect()
        self._ensure_dirs(conn, share, dest_path)
        with open(local_path, "rb") as handle:
            conn.putFile(share, dest_path, handle.read)
        return os.path.getsize(local_path)

    def put_bytes(self, data: bytes, share: str, dest_path: str) -> int:
        conn = self.connect()
        self._ensure_dirs(conn, share, dest_path)
        conn.putFile(share, dest_path, io.BytesIO(data).read)
        return len(data)

    def get_bytes(self, share: str, path: str) -> bytes:
        buf = io.BytesIO()
        self.connect().getFile(share, path, buf.write)
        return buf.getvalue()

    @staticmethod
    def _ensure_dirs(conn: Any, share: str, dest_path: str) -> None:
        """mkdir -p for an SMB path, ignoring 'already exists'."""
        parts = dest_path.replace("/", "\\").split("\\")[:-1]
        walked = ""
        for part in parts:
            if not part:
                continue
            walked = f"{walked}\\{part}" if walked else part
            try:
                conn.createDirectory(share, walked)
            except Exception:  # noqa: BLE001 - benign when the directory exists
                pass


# --------------------------------------------------------------------------
# Execution channels
# --------------------------------------------------------------------------

PROCESS_RUNNING = "RUNNING"
PROCESS_GONE = "GONE"
DEFAULT_CWD = "C:\\"


def _ps_single_quote(value: str) -> str:
    """Escape a value for a PowerShell single-quoted literal."""
    return value.replace("'", "''")


def build_logged_command(command: str, log_path: str) -> str:
    """Wrap a command so all of its output lands in one readable log file.

    The runbook reads phase output back over SMB rather than streaming it, so
    every launch redirects both streams into a log we can fetch afterwards. The
    log directory is created first: cmd's redirection fails outright if the
    parent directory is missing, which otherwise looks like a silent no-op.
    """
    log_dir = log_path.replace("/", "\\").rsplit("\\", 1)[0]
    return f'cmd.exe /c "if not exist "{log_dir}" mkdir "{log_dir}" & {command} > "{log_path}" 2>&1"'


def build_powershell_command(script_path: str) -> str:
    """The invocation used for every phase script on the target."""
    return f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'


class CimChannel:
    """DCOM/WMI channel: ``Win32_Process.Create`` plus PID polling.

    This is the channel the runbook prefers. It works before WinRM is enabled,
    and once ``LocalAccountTokenFilterPolicy=1`` is set it yields a full admin
    token, so install phases run elevated without an interactive UAC prompt.
    """

    def __init__(self, target: Target) -> None:
        self.target = target
        self._dcom: Any = None
        self._services: Any = None

    def _services_handle(self) -> Any:
        if self._services is not None:
            return self._services
        from impacket.dcerpc.v5.dcom import wmi
        from impacket.dcerpc.v5.dcomrt import DCOMConnection
        from impacket.dcerpc.v5.dtypes import NULL

        dcom = DCOMConnection(
            self.target.host,
            self.target.user,
            self.target.password,
            self.target.domain,
            "",
            "",
            "",
            oxidResolver=True,
        )
        interface = dcom.CoCreateInstanceEx(wmi.CLSID_WbemLevel1Login, wmi.IID_IWbemLevel1Login)
        login = wmi.IWbemLevel1Login(interface)
        services = login.NTLMLogin("//./root/cimv2", NULL, NULL)
        login.RemRelease()
        self._dcom = dcom
        self._services = services
        return services

    def close(self) -> None:
        if self._dcom is not None:
            try:
                self._dcom.disconnect()
            finally:
                self._dcom = None
                self._services = None

    def launch(self, command: str, cwd: str | None = None) -> int:
        """Start a detached process. Returns its PID immediately."""
        services = self._services_handle()
        win32_process, _ = services.GetObject("Win32_Process")
        result = win32_process.Create(command, cwd or DEFAULT_CWD, None)
        if result.ReturnValue != 0:
            raise RuntimeError(
                f"Win32_Process.Create failed with ReturnValue={result.ReturnValue} "
                "(2=access denied, 3=insufficient privilege, 8=unknown failure, "
                "9=path not found, 21=invalid parameter)"
            )
        return int(result.ProcessId)

    def poll(self, pid: int) -> str:
        services = self._services_handle()
        query = f"SELECT ProcessId FROM Win32_Process WHERE ProcessId = {int(pid)}"
        enumerator = services.ExecQuery(query)
        try:
            enumerator.Next(0xFFFFFFFF, 1)
            return PROCESS_RUNNING
        except Exception:  # noqa: BLE001 - empty result set means the PID is gone
            return PROCESS_GONE
        finally:
            enumerator.RemRelease()

    def whoami(self) -> dict[str, Any]:
        """Report the identity and elevation WMI grants us on the target."""
        services = self._services_handle()
        enumerator = services.ExecQuery(
            "SELECT UserName, Name, NumberOfLogicalProcessors, TotalPhysicalMemory "
            "FROM Win32_ComputerSystem"
        )
        try:
            record = enumerator.Next(0xFFFFFFFF, 1)[0].getProperties()
            return {key: value.get("value") for key, value in record.items()}
        finally:
            enumerator.RemRelease()


class WinRmChannel:
    """WS-Man channel for short synchronous probes and loopback CIM launches."""

    def __init__(self, target: Target, timeout: int = 45) -> None:
        self.target = target
        self.timeout = timeout
        self._session: Any = None

    def _session_handle(self) -> Any:
        if self._session is not None:
            return self._session
        import winrm

        self._session = winrm.Session(
            self.target.winrm_endpoint,
            auth=(
                f"{self.target.domain}\\{self.target.user}" if self.target.domain else self.target.user,
                self.target.password,
            ),
            transport="ntlm",
            server_cert_validation="ignore",
            operation_timeout_sec=self.timeout,
            read_timeout_sec=self.timeout + 10,
        )
        return self._session

    def run_ps(self, script: str) -> tuple[int, str, str]:
        result = self._session_handle().run_ps(script)
        return (
            result.status_code,
            result.std_out.decode("utf-8", "replace"),
            result.std_err.decode("utf-8", "replace"),
        )

    def launch(self, command: str, cwd: str | None = None) -> int:
        """Detach a process via loopback CIM, so it outlives this WinRM call."""
        escaped = _ps_single_quote(command)
        escaped_cwd = _ps_single_quote(cwd or DEFAULT_CWD)
        script = (
            "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{ CommandLine = '{escaped}'; CurrentDirectory = '{escaped_cwd}' }};"
            "if ($r.ReturnValue -ne 0) { Write-Error \"Create failed: $($r.ReturnValue)\"; exit 1 };"
            "$r.ProcessId"
        )
        code, out, err = self.run_ps(script)
        if code != 0:
            raise RuntimeError(f"loopback CIM launch failed: {err.strip() or out.strip()}")
        return int(out.strip().splitlines()[-1])

    def close(self) -> None:
        """No persistent handle to release; present so callers can treat channels alike."""
        self._session = None

    def poll(self, pid: int) -> str:
        script = (
            f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) "
            f"{{ '{PROCESS_RUNNING}' }} else {{ '{PROCESS_GONE}' }}"
        )
        _, out, _ = self.run_ps(script)
        return PROCESS_GONE if PROCESS_GONE in out else PROCESS_RUNNING


def make_exec_channel(target: Target, channel: str) -> CimChannel | WinRmChannel:
    if channel == "cim":
        return CimChannel(target)
    if channel == "winrm":
        return WinRmChannel(target)
    raise SystemExit(f"unknown channel {channel!r}; expected 'cim' or 'winrm'")


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------


def diagnose(target: Target) -> dict[str, Any]:
    """Probe every channel and report what is usable, in runbook terms.

    The output is deliberately shaped as advice rather than raw errors, because
    the decision it feeds -- "can I install headlessly, or do I need one
    interactive elevated bootstrap first?" -- is the fork the whole install
    hangs off.
    """
    report: dict[str, Any] = {
        "host": target.host,
        "account": f"{target.domain}\\{target.user}" if target.domain else target.user,
        "smb": {},
        "cim": {},
        "winrm": {},
    }

    smb = SmbChannel(target)
    try:
        smb.connect()
        report["smb"]["authenticated"] = True
        # IPC$ authenticating while C$ refuses is the token-filtering signature.
        admin_ok, admin_diag = smb.can_read("C$")
        report["smb"]["admin_share_c"] = ACCESS_OK if admin_ok else admin_diag
        try:
            shares = smb.list_shares()
            report["smb"]["shares"] = shares
            report["smb"]["writable_candidates"] = [
                name
                for name in shares
                if name not in _UNINTERESTING_SHARES and not name.endswith("$")
            ] or [name for name in shares if name in ("Users", "E", "D")]
        except Exception as exc:  # noqa: BLE001 - share enumeration is optional
            report["smb"]["shares_error"] = classify_error(exc)
    except Exception as exc:  # noqa: BLE001 - the diagnosis is the point
        report["smb"]["authenticated"] = False
        report["smb"]["diagnosis"] = classify_error(exc)
        report["smb"]["detail"] = str(exc)
    finally:
        smb.close()

    cim = CimChannel(target)
    try:
        report["cim"] = {"usable": True, "computer_system": cim.whoami()}
    except Exception as exc:  # noqa: BLE001
        report["cim"] = {"usable": False, "diagnosis": classify_error(exc), "detail": str(exc)}
    finally:
        cim.close()

    winrm_channel = WinRmChannel(target, timeout=20)
    try:
        code, out, err = winrm_channel.run_ps(
            "[pscustomobject]@{"
            "  whoami = (whoami);"
            "  elevated = ([Security.Principal.WindowsPrincipal]"
            "    [Security.Principal.WindowsIdentity]::GetCurrent())"
            "    .IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);"
            "  os = (Get-CimInstance Win32_OperatingSystem).Caption"
            "} | ConvertTo-Json -Compress"
        )
        if code == 0:
            report["winrm"] = {"usable": True, "identity": _try_json(out)}
        else:
            report["winrm"] = {"usable": False, "stderr": err.strip()[:400]}
    except Exception as exc:  # noqa: BLE001
        report["winrm"] = {"usable": False, "diagnosis": classify_error(exc), "detail": str(exc)[:400]}

    report["verdict"] = _verdict(report)
    return report


def _try_json(text: str) -> Any:
    try:
        return json.loads(text.strip() or "{}")
    except json.JSONDecodeError:
        return text.strip()[:400]


def _verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Turn the raw probe into the one decision the runbook needs."""
    smb_auth = report["smb"].get("authenticated") is True
    cim_ok = report["cim"].get("usable") is True
    winrm_ok = report["winrm"].get("usable") is True
    smb_diag = report["smb"].get("diagnosis")

    if not smb_auth and smb_diag == ACCESS_BAD_CREDENTIALS:
        return {
            "headless_install_possible": False,
            "reason": "credentials rejected (STATUS_LOGON_FAILURE) - this is a wrong "
            "username/password, not UAC token filtering",
            "next_step": "confirm the account and password with the venue",
        }
    if not smb_auth and smb_diag == ACCESS_UNREACHABLE:
        return {
            "headless_install_possible": False,
            "reason": "SMB port unreachable from here",
            "next_step": "confirm the port-forward and that 445 is open to this source address",
        }
    if cim_ok or winrm_ok:
        return {
            "headless_install_possible": True,
            "reason": f"elevated execution available via {'CIM' if cim_ok else 'WinRM'}",
            "next_step": "proceed to phase 0 recon",
        }
    if smb_auth:
        return {
            "headless_install_possible": False,
            "reason": "credentials authenticate over SMB but no execution channel is "
            "available - the UAC remote-token filtering signature from the runbook",
            "next_step": "run scripts/enable-remoting.bat once interactively (RDP/AnyDesk) "
            "to set LocalAccountTokenFilterPolicy=1 and enable WinRM, then re-probe",
        }
    return {
        "headless_install_possible": False,
        "reason": "no channel established",
        "next_step": "review the per-channel detail above",
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Exposed so tests exercise the real parser, not a copy."""
    parser = argparse.ArgumentParser(
        prog="driver.py",
        description="Headless driver for a SAMePOS venue node install.",
    )
    parser.add_argument("--host", help="target host or IP (default $SAMEPOS_HOST)")
    parser.add_argument("--user", help="admin account, HOST\\name (default $SAMEPOS_USER)")
    parser.add_argument("--smb-port", type=int, help="SMB port (default 445)")
    parser.add_argument("--winrm-port", type=int, help="WinRM port (default 5985)")
    parser.add_argument("--winrm-scheme", choices=("http", "https"), help="WinRM scheme")
    parser.add_argument(
        "--channel",
        choices=("cim", "winrm"),
        default="cim",
        help="execution channel for launch/poll/exec (default cim)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="prove credentials and diagnose the access path")
    sub.add_parser("shares", help="list shares, flagging transfer candidates")

    p_put = sub.add_parser("put", help="upload a file over SMB")
    p_put.add_argument("local")
    p_put.add_argument("--share", required=True, help="share name, e.g. E or Users")
    p_put.add_argument("--dest", required=True, help="path within the share")

    p_get = sub.add_parser("get", help="download a file over SMB (prints it)")
    p_get.add_argument("--share", required=True)
    p_get.add_argument("--path", required=True)
    p_get.add_argument("--out", help="write to this local path instead of stdout")

    p_exec = sub.add_parser("exec", help="run a PowerShell snippet synchronously (WinRM)")
    p_exec.add_argument("script", help="PowerShell to run, or @file to read from a file")

    p_launch = sub.add_parser(
        "launch", help="start a detached phase script; prints its PID at once"
    )
    launch_what = p_launch.add_mutually_exclusive_group(required=True)
    launch_what.add_argument("--script", help="PowerShell script path on the target")
    launch_what.add_argument(
        "--command-line", dest="command_line", help="raw command line instead of --script"
    )
    p_launch.add_argument("--log", required=True, help="log path on the target")
    p_launch.add_argument("--cwd", help="working directory on the target")

    p_poll = sub.add_parser("poll", help="report RUNNING or GONE for a PID")
    p_poll.add_argument("pid", type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target = Target.from_env(args)

    if args.cmd == "probe":
        _emit(diagnose(target))
        return 0

    if args.cmd == "shares":
        smb = SmbChannel(target)
        try:
            shares = smb.list_shares()
            _emit(
                {
                    "shares": shares,
                    "transfer_candidates": [
                        name
                        for name in shares
                        if name not in _UNINTERESTING_SHARES and not name.endswith("$")
                    ],
                    "note": "prefer a non-admin writable share; C$ often refuses even "
                    "after LocalAccountTokenFilterPolicy=1",
                }
            )
        finally:
            smb.close()
        return 0

    if args.cmd == "put":
        smb = SmbChannel(target)
        try:
            size = smb.put(args.local, args.share, args.dest.replace("/", "\\"))
            _emit({"uploaded": args.local, "share": args.share, "dest": args.dest, "bytes": size})
        finally:
            smb.close()
        return 0

    if args.cmd == "get":
        smb = SmbChannel(target)
        try:
            data = smb.get_bytes(args.share, args.path.replace("/", "\\"))
        finally:
            smb.close()
        if args.out:
            with open(args.out, "wb") as handle:
                handle.write(data)
            _emit({"downloaded": args.path, "to": args.out, "bytes": len(data)})
        else:
            sys.stdout.write(data.decode("utf-8", "replace"))
        return 0

    if args.cmd == "exec":
        script = args.script
        if script.startswith("@"):
            with open(script[1:], encoding="utf-8") as handle:
                script = handle.read()
        channel = WinRmChannel(target)
        code, out, err = channel.run_ps(script)
        sys.stdout.write(out)
        if err.strip():
            sys.stderr.write(err)
        return code

    if args.cmd == "launch":
        inner = args.command_line or build_powershell_command(args.script)
        full = build_logged_command(inner, args.log)
        channel = make_exec_channel(target, args.channel)
        try:
            pid = channel.launch(full, args.cwd)
            _emit(
                {
                    "pid": pid,
                    "channel": args.channel,
                    "command": full,
                    "log": args.log,
                    "note": "detached - poll the PID in a separate short call, then fetch the log",
                }
            )
        finally:
            channel.close()
        return 0

    if args.cmd == "poll":
        channel = make_exec_channel(target, args.channel)
        try:
            _emit({"pid": args.pid, "state": channel.poll(args.pid)})
        finally:
            channel.close()
        return 0

    parser.error(f"unhandled command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
