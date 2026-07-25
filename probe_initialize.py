#!/usr/bin/env python3
"""Try every way of calling mt5.initialize() and report which one works.

Use when win_diagnostics.py says Windows is fine (terminal ready, pipe reachable)
but initialize() still returns IPC timeout. That narrows it to how the call is made
or to the terminal build itself, and the fastest way to tell them apart is to try
the variants side by side against every terminal on the machine.

    python probe_initialize.py
    python probe_initialize.py --login 123456 --password secret --server Broker-Live
    python probe_initialize.py --timeout 20000
    python probe_initialize.py --dry-run     # list attempts without touching MT5
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

COMMON_TERMINAL_PATHS = [
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5 Validator\terminal64.exe",
]


def configured_paths() -> list[str]:
    paths: list[str] = []
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, json.JSONDecodeError):
            config = {}
        for key in ("validator_mt5_terminal_path", "mt5_terminal_path"):
            value = config.get(key)
            if value:
                paths.append(str(value))
    return paths


def _unique_existing(paths: list[str]) -> list[str]:
    result: list[str] = []
    lowered: set[str] = set()
    for path in paths:
        if not path:
            continue
        key = path.lower()
        if key in lowered:
            continue
        if Path(path).is_file():
            lowered.add(key)
            result.append(path)
    return result


def build_attempts(paths: list[str], args: argparse.Namespace) -> list[dict]:
    """Each attempt is a label plus the kwargs handed to mt5.initialize()."""
    attempts: list[dict] = [
        {
            "label": "no path (let the package find the terminal)",
            "kwargs": {"timeout": args.timeout},
        }
    ]

    creds = {}
    if args.login and args.password and args.server:
        creds = {"login": int(args.login), "password": args.password, "server": args.server}

    for path in paths:
        attempts.append({"label": f"path={path}", "kwargs": {"path": path, "timeout": args.timeout}})
        forward = path.replace("\\", "/")
        if forward != path:
            attempts.append(
                {
                    "label": f"path with forward slashes={forward}",
                    "kwargs": {"path": forward, "timeout": args.timeout},
                }
            )
        attempts.append(
            {
                "label": f"path={path} portable=True",
                "kwargs": {"path": path, "portable": True, "timeout": args.timeout},
            }
        )
        if creds:
            attempts.append(
                {
                    "label": f"path={path} with credentials in initialize()",
                    "kwargs": {"path": path, "timeout": args.timeout, **creds},
                }
            )

    if creds:
        attempts.append(
            {
                "label": "credentials only, no path",
                "kwargs": {"timeout": args.timeout, **creds},
            }
        )
    return attempts


def describe(kwargs: dict) -> str:
    shown = {k: ("***" if k == "password" else v) for k, v in kwargs.items()}
    return ", ".join(f"{k}={v!r}" for k, v in shown.items())


def run(attempts: list[dict]) -> list[dict]:
    import MetaTrader5 as mt5

    print(f"MetaTrader5 package version: {getattr(mt5, '__version__', 'unknown')}\n")
    results = []
    for index, attempt in enumerate(attempts, start=1):
        print(f"[{index}/{len(attempts)}] {attempt['label']}")
        print(f"        initialize({describe(attempt['kwargs'])})")
        try:
            ok = bool(mt5.initialize(**attempt["kwargs"]))
            error = None if ok else mt5.last_error()
            detail = None
            if ok:
                info = mt5.terminal_info()
                if info is not None:
                    detail = {
                        "build": getattr(info, "build", None),
                        "connected": getattr(info, "connected", None),
                        "path": getattr(info, "path", None),
                    }
        except Exception as exc:  # noqa: BLE001 - a probe must never abort early
            ok, error, detail = False, repr(exc), None
        finally:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass

        print(f"        -> {'OK' if ok else 'FAILED'}{'' if ok else f' {error}'}")
        if detail:
            print(f"        -> terminal {detail}")
        print()
        results.append({**attempt, "ok": ok, "error": error, "terminal": detail})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", action="append", default=[], help="extra terminal64.exe to test")
    parser.add_argument("--login")
    parser.add_argument("--password")
    parser.add_argument("--server")
    parser.add_argument(
        "--timeout",
        type=int,
        default=10_000,
        help="per-attempt timeout in ms (keep low; every failure costs this long)",
    )
    parser.add_argument("--dry-run", action="store_true", help="list attempts without calling MT5")
    args = parser.parse_args()

    paths = _unique_existing([*args.path, *configured_paths(), *COMMON_TERMINAL_PATHS])
    if not paths:
        print("No terminal64.exe found from --path, config.json or the common install paths.")

    attempts = build_attempts(paths, args)

    if args.dry_run:
        print(f"{len(attempts)} attempts planned against {len(paths)} terminal(s):\n")
        for index, attempt in enumerate(attempts, start=1):
            print(f"[{index}] {attempt['label']}")
            print(f"    initialize({describe(attempt['kwargs'])})")
        return 0

    results = run(attempts)
    working = [r for r in results if r["ok"]]

    print("=" * 70)
    if working:
        print("Working call(s):")
        for result in working:
            print(f"  - {result['label']}")
        print("\nUse the first one in validator_server.py / config.json.")
    else:
        print("Every variant failed.")
        print(
            "With the terminal ready and its pipe reachable, that points at the terminal build "
            "refusing IPC. Two things to try, in order:\n"
            "  1. In the terminal: Tools > Options > Community, enable Python integration if the\n"
            "     option exists, then restart the terminal.\n"
            "  2. Install the original terminal from https://www.metatrader5.com/en/download\n"
            "     (broker-customised builds can ship with IPC blocked), point\n"
            "     validator_mt5_terminal_path at it, and add the broker server inside it."
        )
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(main())
