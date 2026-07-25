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

# Standard Win32 dialog class; an MT5 modal (account wizard, EULA, update) shows up as this.
DIALOG_WINDOW_CLASS = "#32770"


def powershell_json(command: str, timeout: int = 25):
    """Run a PowerShell one-liner that ends in ConvertTo-Json and parse the result."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"query failed: {exc}"

    output = (proc.stdout or "").strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return f"unexpected output: {output[:200]}"


def _as_list(parsed) -> list | str:
    if parsed is None:
        return []
    if isinstance(parsed, str):
        return parsed
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


def running_terminals(process_name: str = TERMINAL_PROCESS_NAME) -> list[dict] | str:
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
    if isinstance(terminals, str):
        return terminals

    for terminal in terminals:
        pid = terminal.get("ProcessId")
        if isinstance(pid, int):
            terminal["Elevated"] = process_elevated(pid)
            terminal["Windows"] = _notable_windows(process_windows(pid))
    return terminals


def _notable_windows(windows: list[dict] | str) -> list[dict] | str:
    """Drop the dozens of invisible helper windows every GUI app owns."""
    if isinstance(windows, str):
        return windows
    return [
        w
        for w in windows
        if w.get("visible") and (w.get("title") or w.get("class") == DIALOG_WINDOW_CLASS)
    ]


def process_windows(pid: int) -> list[dict] | str:
    """Top-level windows owned by a process, to spot a blocking modal dialog.

    Only sees windows on the caller's own desktop, which is exactly the condition
    the MT5 pipe also requires.
    """
    try:
        user32 = ctypes.windll.user32
        found: list[dict] = []
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
        return found
    except (AttributeError, OSError) as exc:
        return f"window list unavailable: {exc}"


def mt5_pipes() -> list[str] | str:
    """Named pipes that look like MetaTrader's.

    A terminal that has finished starting publishes one. If none exists the
    terminal is up but not ready, so no client could ever connect.
    """
    pipes = _as_list(
        powershell_json(
            "[System.IO.Directory]::GetFiles('\\\\.\\pipe\\') "
            "| Where-Object { $_ -match 'MT5|MetaTrader|MQL' } | ConvertTo-Json -Compress"
        )
    )
    if isinstance(pipes, str):
        return pipes
    return [str(p) for p in pipes]


def file_version(path: str) -> str | None:
    escaped = path.replace("'", "''")
    result = powershell_json(
        f"(Get-Item -LiteralPath '{escaped}').VersionInfo "
        "| Select-Object FileVersion,ProductVersion | ConvertTo-Json -Compress"
    )
    if isinstance(result, dict):
        return result.get("FileVersion") or result.get("ProductVersion")
    return result if isinstance(result, str) else None


def build_number(version: str | None) -> int | None:
    """Terminal build from a file version like '5.0.0.6061' -> 6061."""
    if not version:
        return None
    parts = [p for p in version.replace(",", ".").split(".") if p.strip().isdigit()]
    return int(parts[-1]) if parts else None


def ipc_timeout_hints(terminal_path: str | None) -> list[str]:
    """The specific, evidence-backed reasons the pipe is unreachable."""
    hints: list[str] = []
    session = session_info()
    terminals = running_terminals()

    if isinstance(terminals, str):
        return [terminals]

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
        mismatched = [t for t in ours if t.get("Elevated") is not None and t["Elevated"] != our_elevation]
        if mismatched:
            hints.append(
                f"Elevation mismatch: the validator is {'elevated' if our_elevation else 'not elevated'} "
                f"but terminal PID {mismatched[0].get('ProcessId')} is not. Integrity level blocks "
                "the pipe - start both the same way."
            )

    build = build_number(file_version(terminal_path)) if terminal_path else None
    if build is not None and build < MIN_IPC_BUILD:
        hints.append(
            f"Terminal build {build} is older than {MIN_IPC_BUILD} and has no Python IPC support."
        )

    dialogs = [
        w
        for t in ours
        if isinstance(t.get("Windows"), list)
        for w in t["Windows"]
        if w.get("class") == DIALOG_WINDOW_CLASS and w.get("visible")
    ]
    if dialogs:
        hints.append(
            "A modal dialog is open on the terminal and blocking startup: "
            f"{[d.get('title') or '(untitled)' for d in dialogs]}. Dismiss it on the desktop."
        )

    pipes = mt5_pipes()
    if isinstance(pipes, str):
        hints.append(f"Could not enumerate named pipes: {pipes}")
    elif not pipes:
        hints.append(
            "The terminal has not published a named pipe, so it never finished starting. "
            "Open it on the desktop, complete any first-run dialog, log into an account with "
            "'Save account information', and wait for a ping in the status bar."
        )
    else:
        hints.append(
            f"Named pipes exist ({pipes}) but the connection still timed out, which points at "
            "permissions/integrity rather than terminal readiness."
        )

    return hints


def report(terminal_path: str | None) -> dict:
    version = file_version(terminal_path) if terminal_path else None
    return {
        "terminal_path": terminal_path,
        "terminal_version": version,
        "terminal_build": build_number(version),
        "validator_session": session_info(),
        "running_terminals": running_terminals(),
        "mt5_pipes": mt5_pipes(),
    }


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps(report(path), indent=2, default=str))
    print()
    for hint in ipc_timeout_hints(path):
        print("-", hint)
