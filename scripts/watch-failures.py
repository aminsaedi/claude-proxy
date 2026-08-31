#!/usr/bin/env python3
"""Watch the proxy for requests that did not reach the caller.

Anything that is not a completed answer shows up here: an upstream refusal, a
stream that carried an `error` frame, a completion cut short, a caller that
hung up, a blocked budget, a bad key.

Two things about this are worth knowing before you trust it.

**"No output" is only good news if the audit log is whole.** A request can also
disappear because the audit queue overflowed and dropped it. That counter is
polled alongside the rows and reported the moment it moves, because otherwise
silence here would be ambiguous in exactly the way that hid the original
timeouts for weeks.

**It cannot see what never arrived.** If the edge answers on the proxy's behalf
— a Cloudflare 524, a 502 from something in front — there is no row to find and
there never will be. A quiet watcher plus a client still reporting errors means
the fault is in front of this process, and Cloudflare's own analytics is the
next place to look, not this log.

    python scripts/watch-failures.py http://127.0.0.1:8090          # follow
    python scripts/watch-failures.py <base> --since 24 --once       # summary

`--once` exits 1 when it found failures, so it can drive an alert or a CI gate.
Reads ADMIN_USER / ADMIN_PASSWORD from the environment when the target has auth.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
FLAGS = [a for a in sys.argv[1:] if a.startswith("--")]
BASE = (ARGS[0] if ARGS else "http://127.0.0.1:8090").rstrip("/")
ONCE = "--once" in FLAGS
QUIET = "--quiet" in FLAGS


def _flag(name: str, default: float) -> float:
    for f in FLAGS:
        if f.startswith(f"--{name}="):
            return float(f.split("=", 1)[1])
    return default


SINCE_HOURS = _flag("since", 1.0)
INTERVAL = _flag("interval", 5.0)

# Terminal colour, but only when someone is actually watching; piped into a file
# or a log collector the escape codes are just noise.
_TTY = sys.stdout.isatty()
DIM, RED, YEL, RST = ("\033[2m", "\033[31m", "\033[33m", "\033[0m") if _TTY else ("", "", "", "")
# How loud each outcome is. `blocked` and `rejected` are working as designed —
# a spend cap doing its job is not an incident — so they are reported without
# being alarming, and they do not set the exit code.
SEVERITY = {"error": RED, "incomplete": RED, "aborted": YEL, "blocked": DIM, "rejected": DIM}
BENIGN = {"blocked", "rejected"}


def get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}")
    user, password = os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASSWORD")
    if user and password:
        raw = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {raw}")
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - operator-supplied base
        return json.loads(r.read())


def describe(row: dict) -> str:
    when = datetime.fromtimestamp(row["ts"]).strftime("%H:%M:%S")
    outcome = row.get("outcome") or "?"
    colour = SEVERITY.get(outcome, RED)
    latency = f"{row['latency_ms'] / 1000:6.1f}s" if row.get("latency_ms") else "     —"
    ttfb = f"ttfb {row['ttfb_ms'] / 1000:.1f}s" if row.get("ttfb_ms") else "never forwarded"
    error = (row.get("error") or "").replace("\n", " ")[:110]
    # A rejected request has no virtual key by definition, and naming the
    # column "—" for every one of them wastes the width that could be
    # telling you where they are coming from.
    who = row.get("key_name") or row.get("client_ip") or "—"
    return (f"{DIM}{when}{RST} {colour}{outcome:<10}{RST} "
            f"{str(row.get('status') or '—'):>4} {latency} {ttfb:<16} "
            f"{who:<24} {(row.get('model') or '—'):<24} "
            f"{row.get('request_id', '')[:8]} {colour}{error}{RST}")


def main() -> int:
    try:
        stats = get("/audit/stats")
    except (urllib.error.URLError, OSError) as e:
        print(f"{RED}cannot reach {BASE}: {e}{RST}", file=sys.stderr)
        return 2
    if stats.get("mode") == "off":
        print(f"{RED}audit logging is off — there is nothing to watch{RST}", file=sys.stderr)
        return 2

    since = time.time() - SINCE_HOURS * 3600
    rows = get(f"/requests?failed=1&limit=500&since={since:.0f}")["requests"]
    rows.reverse()  # oldest first: this is a log, not a leaderboard

    window = f"{SINCE_HOURS:g}h"
    print(f"{DIM}{BASE} · audit={stats['mode']} · {stats['rows']} rows retained · "
          f"scanning last {window}{RST}")
    for row in rows:
        print(describe(row))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get("outcome") or "?"] = counts.get(row.get("outcome") or "?", 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing"
    print(f"{DIM}— {summary} in the last {window}{RST}")
    # One broken client retrying in a loop produces the same row count as a
    # proxy-wide outage; the difference is entirely in this breakdown.
    if len(rows) > 5:
        by_source: dict[str, int] = {}
        for row in rows:
            src = row.get("key_name") or row.get("client_ip") or "?"
            by_source[src] = by_source.get(src, 0) + 1
        top = sorted(by_source.items(), key=lambda kv: -kv[1])[:5]
        print(f"{DIM}— from: " + ", ".join(f"{s} ({n})" for s, n in top) + RST)

    dropped = stats.get("dropped", 0)
    if dropped:
        print(f"{RED}audit dropped {dropped} record(s) — some requests have no row "
              f"at all, so this list is incomplete{RST}")

    if ONCE:
        real = sum(n for k, n in counts.items() if k not in BENIGN)
        return 1 if (real or dropped) else 0

    # Follow. `after_id` is a keyset cursor, so each poll returns only what has
    # landed since the last one however busy the proxy gets.
    cursor = max((r["id"] for r in rows), default=0)
    if not cursor:
        cursor = max((r["id"] for r in get("/requests?limit=1")["requests"]), default=0)
    print(f"{DIM}watching for new failures (Ctrl-C to stop)…{RST}")
    while True:
        try:
            time.sleep(INTERVAL)
            fresh = get(f"/requests?failed=1&limit=500&after_id={cursor}")["requests"]
            for row in reversed(fresh):
                print(describe(row), flush=True)
            newest = get("/requests?limit=1")["requests"]
            cursor = max([cursor] + [r["id"] for r in newest] + [r["id"] for r in fresh])
            now = get("/audit/stats").get("dropped", 0)
            if now > dropped:
                print(f"{RED}audit dropped {now - dropped} more record(s) — "
                      f"failures may be going unrecorded{RST}", flush=True)
                dropped = now
        except KeyboardInterrupt:
            return 0
        except (urllib.error.URLError, OSError) as e:
            # A rollout makes the admin endpoint briefly unreachable. That is
            # not a reason to stop watching.
            if not QUIET:
                print(f"{YEL}poll failed ({e}) — retrying{RST}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
