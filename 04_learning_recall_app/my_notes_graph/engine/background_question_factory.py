"""Network-aware background question pre-generator.

The quiz loop itself never calls the internet: it reads ``quiz_bank`` from
SQLite.  This daemon only tops that bank up while the host is online, in small
micro-batches, and writes a status file the dashboard can display.

Because generation spends metered data, the daemon is **opt-in**: it is never
started by the web app or by ``run_app.sh``.  Run it explicitly with
``bash run_factory.sh`` when you want the buffer refilled.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import DATA

from . import db, quiz_factory

STATUS_FILE = DATA / "factory_status.json"
DEFAULT_TARGET = 500
DEFAULT_BATCH = 5
DEFAULT_INTERVAL = 300.0
MAX_BACKOFF = 900.0


def internet_available(host: str = "8.8.8.8", port: int = 53, timeout: float = 2.0) -> bool:
    """Probe reachability with a single TCP connect; never downloads anything."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def staged_count(path: str | None = None) -> int:
    """Questions that are still available to serve (staged or on a device)."""
    connection = db.open_database(path)
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM quiz_bank WHERE status IN ('STAGED','SYNCED_TO_MOBILE')"
        ).fetchone()[0])
    finally:
        connection.close()


def _write_status(payload: dict) -> dict:
    """Atomically publish the daemon state for ``/api/quiz/factory/status``."""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2))
    os.replace(temporary, STATUS_FILE)
    return payload


def read_status() -> dict:
    """Return the last published state, or a neutral offline record."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"running": False, "online": False, "staged": 0,
                "target": DEFAULT_TARGET, "updated_at": ""}


def generate_once(*, target: int = DEFAULT_TARGET, batch: int = DEFAULT_BATCH,
                  max_batches: int | None = None, path: str | None = None,
                  probe=internet_available, factory_factory=quiz_factory.QuizFactory,
                  sleep=time.sleep, rng: random.Random | None = None,
                  max_attempts: int = 3) -> dict:
    """Top the staged pool up to ``target`` in one bounded pass."""
    generator = rng or random.Random()
    staged = staged_count(path)
    if staged >= target:
        return _write_status({"running": False, "online": None, "staged": staged,
                              "target": target, "inserted": 0,
                              "message": "buffer already full"})
    if not probe():
        return _write_status({"running": False, "online": False, "staged": staged,
                              "target": target, "inserted": 0,
                              "message": "offline; nothing generated"})
    factory = factory_factory(path=path)
    inserted = 0
    errors: list[str] = []
    attempts = 0
    while staged + inserted < target and attempts < max_attempts:
        attempts += 1
        deficit = target - (staged + inserted)
        report = factory.run(factory.bank_size() + deficit, per_batch=batch,
                             max_batches=max_batches, verbose=False)
        inserted += int(report.inserted)
        errors.extend(report.errors[-3:])
        if report.inserted:
            break
        delay = min(MAX_BACKOFF, 30.0 * (2 ** (attempts - 1))) + generator.uniform(0, 10)
        sleep(delay)
    return _write_status({
        "running": False, "online": True, "staged": staged + inserted,
        "target": target, "inserted": inserted, "attempts": attempts,
        "errors": errors[-5:],
    })


def daemon(*, target: int = DEFAULT_TARGET, batch: int = DEFAULT_BATCH,
           interval: float = DEFAULT_INTERVAL, path: str | None = None,
           probe=internet_available, factory_factory=quiz_factory.QuizFactory,
           sleep=time.sleep, rng: random.Random | None = None) -> None:
    """Poll connectivity and refill the buffer whenever the host is online."""
    generator = rng or random.Random()
    while True:
        try:
            result = generate_once(target=target, batch=batch, path=path,
                                   probe=probe, factory_factory=factory_factory,
                                   sleep=sleep, rng=generator)
        except Exception as error:  # keep the daemon alive across transient faults
            result = _write_status({"running": False, "online": True,
                                    "staged": staged_count(path), "target": target,
                                    "error": f"{type(error).__name__}: {error}"})
        wait = max(30.0, float(interval)) + generator.uniform(0, 30)
        _write_status({**result, "running": True, "next_check_in": round(wait, 1)})
        sleep(wait)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    parser.add_argument("--daemon", action="store_true", help="poll and refill forever")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET,
                        help="desired staged pool size (default 500)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, choices=range(4, 7))
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--path", help="alternate recall database")
    parser.add_argument("--dry-run", action="store_true",
                        help="report state without calling the model")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps({"online": internet_available(), "staged": staged_count(args.path),
                          "target": args.target}, indent=2))
        return 0
    if args.daemon:
        daemon(target=args.target, batch=args.batch, interval=args.interval, path=args.path)
        return 0
    result = generate_once(target=args.target, batch=args.batch,
                           max_batches=args.max_batches, path=args.path)
    print(json.dumps(result, indent=2))
    return 0 if result.get("inserted", 0) or result.get("staged", 0) >= args.target else 1


if __name__ == "__main__":
    raise SystemExit(main())
