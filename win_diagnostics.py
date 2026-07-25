"""Windows-level facts needed to explain an MT5 'IPC timeout'.

Deliberately free of any MetaTrader5 import so it can be run on its own:

    python win_diagnostics.py "C:\\eqt\\mt5-validator\\terminal64.exe"
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from ctypes import wintypes

TERMINAL_PROCESS_NAME = "terminal64.exe"

# Python IPC support was added in terminal build 2085.
MIN_IPC_BUILD = 2085

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_PIPE_BUSY = 231

# Standard Win32 dialog class; an MT5 modal (account wizard, EULA, update) shows up as this.
DIALOG_WINDOW_CLASS = "#32770"


class DiagnosticsError(RuntimeError):
    """A diagnostic query could not be completed (as opposed to returning 'nothing')."""


def powershell_json(command: str, timeout: int = 25):
    """Run a PowerShell one-liner ending in ConvertTo-Json and parse the result.

    Returns None when the command produced no output. Raises DiagnosticsError if
    the query itself failed, so an error can never be mistaken for a result.
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiagnosticsError(f"query failed: {exc}") from exc

    output = (proc.stdout or "").strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise DiagnosticsError(f"unexpected output: {output[:200]}") from exc


def _as_list(parsed) -> list:
    """ConvertTo-Json collapses a single result to a scalar; restore the list."""
    if parsed is None:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def session_info() -> dict:
    """Session, user and elevation of the current process."""
    info: dict = {"pid": os.getpid(), "user": os.environ.get("USERNAME"), "session_id": None}
    try:
        session_id = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
            info["session_id"] = int(session_id.value)
            info["interactive_desktop"] = info["session_id"] != 0
    except (AttributeError, OSError):
        pass
    info["elevated"] = process_elevated(os.getpid())
    return info


def process_elevated(pid: int) -> bool | None:
    """True/False if we can read the process token, None if access is denied."""
    try:
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        token = wintypes.HANDLE()
        try:
            if not advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
                return None
            elevation = wintypes.DWORD()
            returned = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                token,
                _TOKEN_ELEVATION,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(returned),
            )
            return bool(elevation.value) if ok else None
        finally:
            if token.value:
                kernel32.CloseHandle(token)
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


def running_terminals(process_name: str = TERMINAL_PROCESS_NAME) -> list[dict]:
    """Live terminals with the attributes that decide whether the pipe is reachable."""
    terminals = _as_list(
        powershell_json(
            f"Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" | ForEach-Object {{ "
            "[pscustomobject]@{ ProcessId=$_.ProcessId; SessionId=$_.SessionId; "
            "ExecutablePath=$_.ExecutablePath; "
            "Owner=(Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User } } "
            "| ConvertTo-Json -Compress"
        )
    )
    for terminal in terminals:
        pid = terminal.get("ProcessId")
        if isinstance(pid, int):
            terminal["Elevated"] = process_elevated(pid)
            terminal["Windows"] = _notable_windows(process_windows(pid))
    return terminals


def _notable_windows(windows: list[dict]) -> list[dict]:
    """Drop the dozens of invisible helper windows every GUI app owns."""
    return [
        w
        for w in windows
        if w.get("visible") and (w.get("title") or w.get("class") == DIALOG_WINDOW_CLASS)
    ]


def process_windows(pid: int) -> list[dict]:
    """Top-level windows owned by a process, to spot a blocking modal dialog.

    Only sees windows on the caller's own desktop, which is exactly the condition
    the MT5 pipe also requires.
    """
    found: list[dict] = []
    try:
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def on_window(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != int(pid):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 256)
            found.append(
                {
                    "title": title.value,
                    "class": class_name.value,
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                }
            )
            return True

        user32.EnumWindows(callback_type(on_window), 0)
    except (AttributeError, OSError) as exc:
        raise DiagnosticsError(f"window list unavailable: {exc}") from exc
    return found


def mt5_pipes() -> list[str]:
    """Named pipes that look like MetaTrader's.

    A terminal that has finished starting publishes one. If none exists, the
    terminal is up but not ready, so no client could connect.
    """
    pipes = _as_list(
        powershell_json(
            "[System.IO.Directory]::GetFiles('\\\\.\\pipe\\') "
            "| Where-Object { $_ -match 'MT5|MetaTrader|MQL' } | ConvertTo-Json -Compress"
        )
    )
    return [str(p) for p in pipes]


def probe_pipe(name: str) -> dict:
    """Try to open a named pipe, to separate 'not reachable' from 'not answering'.

    Opens and immediately closes one pipe instance. If this succeeds, the pipe is
    reachable from here and any IPC timeout is in the protocol handshake above it
    rather than in Windows permissions.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        handle = kernel32.CreateFileW(
            name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error() or kernel32.GetLastError()
            return {
                "pipe": name,
                "opened": False,
                "error_code": int(code),
                "error": ctypes.FormatError(int(code)).strip(),
                # A busy pipe still proves it exists and permits us.
                "exists_but_busy": int(code) == _ERROR_PIPE_BUSY,
            }
        kernel32.CloseHandle(handle)
        return {"pipe": name, "opened": True}
    except (AttributeError, OSError) as exc:
        return {"pipe": name, "opened": False, "error": str(exc)}


def file_version(path: str) -> str | None:
    escaped = path.replace("'", "''")
    result = powershell_json(
        f"(Get-Item -LiteralPath '{escaped}').VersionInfo "
        "| Select-Object FileVersion,ProductVersion | ConvertTo-Json -Compress"
    )
    if isinstance(result, dict):
        return result.get("FileVersion") or result.get("ProductVersion")
    return None


def build_number(version: str | None) -> int | None:
    """Terminal build from a version like '5.0.0.6061' -> 6061."""
    if not version:
        return None
    parts = [p for p in str(version).replace(",", ".").split(".") if p.strip().isdigit()]
    return int(parts[-1]) if parts else None


def ipc_timeout_hints(terminal_path: str | None, package_version: str | None = None) -> list[str]:
    """The specific, evidence-backed reasons the pipe is unreachable."""
    try:
        return _ipc_timeout_hints(terminal_path, package_version)
    except DiagnosticsError as exc:
        return [f"Diagnostics incomplete: {exc}"]


def _ipc_timeout_hints(terminal_path: str | None, package_version: str | None) -> list[str]:
    hints: list[str] = []
    session = session_info()
    terminals = running_terminals()

    if not terminals:
        return [
            "No terminal64.exe is running: it is failing to start or exiting immediately "
            "(missing files in a copied install, or blocked by antivirus)."
        ]

    ours = [
        t
        for t in terminals
        if terminal_path
        and str(t.get("ExecutablePath") or "").lower() == str(terminal_path).lower()
    ]
    if not ours:
        return [
            "A terminal is running, but not the one configured as validator_mt5_terminal_path: "
            f"found {[t.get('ExecutablePath') for t in terminals]}."
        ]

    our_session = session.get("session_id")
    if our_session is not None:
        other_sessions = {t.get("SessionId") for t in ours if t.get("SessionId") != our_session}
        if other_sessions and len(other_sessions) == len({t.get("SessionId") for t in ours}):
            hints.append(
                f"The terminal runs in Windows session {sorted(other_sessions)} but the validator "
                f"is in session {our_session}. The IPC pipe does not cross sessions."
            )

    our_user = (session.get("user") or "").lower()
    owners = {str(t.get("Owner") or "").lower() for t in ours if t.get("Owner")}
    if our_user and owners and our_user not in owners:
        hints.append(
            f"The terminal is owned by {sorted(owners)} but the validator runs as "
            f"{session.get('user')}. Both must run under the same Windows user."
        )

    # Only a genuine mismatch matters; both elevated or both not is fine.
    our_elevation = session.get("elevated")
    if our_elevation is not None:
        mismatched = [
            t for t in ours if t.get("Elevated") is not None and t["Elevated"] != our_elevation
        ]
        if mismatched:
            hints.append(
                f"Elevation mismatch: the validator is "
                f"{'elevated' if our_elevation else 'not elevated'} but terminal PID "
                f"{mismatched[0].get('ProcessId')} is not. Integrity level blocks the pipe - "
                "start both the same way."
            )

    terminal_build = build_number(file_version(terminal_path)) if terminal_path else None
    if terminal_build is not None and terminal_build < MIN_IPC_BUILD:
        hints.append(
            f"Terminal build {terminal_build} is older than {MIN_IPC_BUILD} and has no Python "
            "IPC support."
        )

    dialogs = [
        w
        for t in ours
        for w in t.get("Windows", [])
        if w.get("class") == DIALOG_WINDOW_CLASS and w.get("visible")
    ]
    if dialogs:
        hints.append(
            "A modal dialog is open on the terminal and blocking startup: "
            f"{[d.get('title') or '(untitled)' for d in dialogs]}. Dismiss it on the desktop."
        )

    pipes = mt5_pipes()
    if not pipes:
        hints.append(
            "The terminal has not published a named pipe, so it never finished starting. "
            "Open it on the desktop, complete any first-run dialog, log into an account with "
            "'Save account information', and wait for a ping in the status bar."
        )
        return hints

    probes = [probe_pipe(p) for p in pipes]
    reachable = [p for p in probes if p.get("opened") or p.get("exists_but_busy")]
    if not reachable:
        hints.append(
            f"The terminal's pipe exists but cannot be opened from here: {probes}. "
            "That is a permissions/integrity problem rather than terminal readiness."
        )
        return hints

    hints.append(
        f"The terminal is ready and its pipe is reachable ({[p['pipe'] for p in reachable]}), "
        "so Windows is not the problem: the terminal is refusing the IPC handshake."
    )

    package_build = build_number(package_version)
    if package_build is not None and terminal_build is not None and package_build < terminal_build:
        hints.append(
            f"The package is build {package_build} and the terminal is build {terminal_build}. "
            f"If pip has nothing newer than {package_version}, the package is not the problem - "
            "the terminal is simply a newer/customised build."
        )

    hints.append(
        "In the terminal, check Tools > Options > Community for a Python integration option and "
        "enable it, then restart the terminal."
    )
    hints.append(
        "Broker-customised terminals are sometimes built with IPC disabled. Install the original "
        "terminal from https://www.metatrader5.com/en/download, point "
        "validator_mt5_terminal_path at it, and add the broker's server inside it."
    )
    hints.append(
        "To find a call that does work, run: python probe_initialize.py"
    )

    return hints


def report(terminal_path: str | None) -> dict:
    """Everything above, tolerant of individual queries failing."""
    data: dict = {"terminal_path": terminal_path}

    try:
        version = file_version(terminal_path) if terminal_path else None
        data["terminal_version"] = version
        data["terminal_build"] = build_number(version)
    except DiagnosticsError as exc:
        data["terminal_version_error"] = str(exc)

    data["validator_session"] = session_info()

    try:
        data["running_terminals"] = running_terminals()
    except DiagnosticsError as exc:
        data["running_terminals_error"] = str(exc)

    try:
        pipes = mt5_pipes()
        data["mt5_pipes"] = pipes
        data["mt5_pipe_probes"] = [probe_pipe(p) for p in pipes]
    except DiagnosticsError as exc:
        data["mt5_pipes_error"] = str(exc)

    return data


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None

    package_version = None
    try:
        import MetaTrader5

        package_version = getattr(MetaTrader5, "__version__", None)
    except ImportError:
        pass

    print(json.dumps({**report(path), "mt5_package_version": package_version}, indent=2, default=str))
    print()
    for hint in ipc_timeout_hints(path, package_version):
        print("-", hint)
