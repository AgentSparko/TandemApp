"""Recorded decision-provider answers for a replay (SRV-851).

    python tools/decision_replay.py --ledger <usage ledger file> [--hours 24] [--provider ID]
        > answers.json
    TANDEM_DECISION_REPLAY=answers.json <the controller>       every provider answers from it

The controller journals every provider call (the conductor's `conductor_events`, kind `provider`)
with a hash of the question's text, wording and options and the answer it got. This reads those
rows (read-only) and writes the answers by hash; a controller started with the file answers the
same question with the same answer and no call, and a question it has no answer for falls back
to the built-in as a provider failure would (`replay_miss`). The text itself is never in the
journal or the file.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def rows(path: str, hours: float, provider: str | None) -> list[dict]:
    since = int(time.time() * 1000 - hours * 3600 * 1000)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        out = []
        for (detail,) in con.execute("SELECT detail FROM conductor_events WHERE kind = 'provider' "
                                     "AND ts_ms >= ? ORDER BY ts_ms", (since,)):
            try:
                d = json.loads(detail or "{}")
            except ValueError:
                continue
            if provider is None or d.get("provider") == provider:
                out.append(d)
        return out
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ledger", required=True, help="the usage ledger's file (its journal)")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--provider", default=None)
    a = ap.parse_args(argv)
    from proxy.providers import Recorded
    rec = Recorded.from_records(rows(a.ledger, a.hours, a.provider))
    json.dump(rec.dump(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    print(f"[decision-replay] {len(rec.answers)} answers", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
