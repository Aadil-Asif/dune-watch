#!/usr/bin/env python3
"""Always-on watcher for newly published Odyssey IMAX 70mm dates.

Theatres publish their schedule forward in time, so the interesting event is
the *frontier* moving: the first day past the last day that currently has
showtimes. Probing just that edge costs a couple of requests per cycle instead
of re-sweeping the whole horizon, which is what lets us poll every ~90s
without hammering Fandango.

Two loops share one process:

  frontier probe  every ~90s   - the few days past the known edge. When one
                                 lands, walk forward until we hit empty days
                                 again, so a whole newly-published week is
                                 caught in a single cycle.
  full sweep      every ~30min - today..frontier, to catch showtimes inserted
                                 into days that were already published.

Run it with run-watcher.cmd and leave it running. Ctrl+C saves state and exits.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import date, datetime, timedelta

import watch as W

FRONTIER_INTERVAL = int(os.environ.get("FRONTIER_INTERVAL", "90"))
FULL_SWEEP_INTERVAL = int(os.environ.get("FULL_SWEEP_INTERVAL", "1800"))
# How many days past the frontier to probe, so a single dark day (theatre not
# showing the film that date) doesn't stall the edge forever.
PROBE_LOOKAHEAD = int(os.environ.get("PROBE_LOOKAHEAD", "2"))
# Cap on how far one cycle will walk forward after a hit.
MAX_WALK = int(os.environ.get("MAX_WALK", "10"))
SEED_DAYS = int(os.environ.get("SEED_DAYS", "30"))
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "24"))

STATE_PATH = os.environ.get("STATE_PATH", "daemon_state.json")

_stop = False


def _handle_stop(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    print("\nStopping after current cycle...")


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


# --- state ------------------------------------------------------------------


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            st = json.load(fh)
            st.setdefault("frontier", {})
            st.setdefault("showtimes", {})
            return st
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 2, "seeded": False, "frontier": {}, "showtimes": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, STATE_PATH)  # atomic, so a kill mid-write can't corrupt it


def d(iso: str) -> date:
    return datetime.strptime(iso, "%Y-%m-%d").date()


def frontier_for(state: dict, key: str) -> date:
    """Last day known to have showtimes at this theatre (default: yesterday)."""
    raw = state["frontier"].get(key)
    return d(raw) if raw else date.today() - timedelta(days=1)


def record(state: dict, hits: dict[str, dict]) -> list[dict]:
    """Merge hits into state; return those we'd never seen before."""
    fresh = []
    for uid, show in hits.items():
        if uid not in state["showtimes"]:
            fresh.append(show)
        state["showtimes"][uid] = show
        key, day = show["theater_key"], d(show["date"])
        if day > frontier_for(state, key):
            state["frontier"][key] = show["date"]
    return fresh


def probe_day(target: dict, day: date) -> dict[str, dict] | None:
    iso = day.isoformat()
    vm = W.fetch_day(target, iso)
    if vm is None:
        return None
    return W.extract(vm, target, iso)


# --- the two loops ----------------------------------------------------------


def frontier_probe(state: dict, dry_run: bool) -> list[dict]:
    """Look just past the edge; on a hit, keep walking while days keep landing."""
    fresh: list[dict] = []
    for target in W.TARGETS:
        key = target["key"]
        walked = 0
        while walked < MAX_WALK and not _stop:
            edge = frontier_for(state, key)
            hits: dict[str, dict] = {}
            for offset in range(1, PROBE_LOOKAHEAD + 1):
                day = edge + timedelta(days=offset)
                got = probe_day(target, day)
                if got:
                    hits.update(got)
                time.sleep(W.REQUEST_DELAY)
            if not hits:
                break
            new = record(state, hits)
            if new:
                log(f"  {key}: +{len(new)} showtime(s) past {edge.isoformat()}")
                fresh.extend(new)
            walked += 1
            if frontier_for(state, key) <= edge:
                break  # hits were all on days we already had; don't spin
    return fresh


def full_sweep(state: dict, days_ahead: int | None = None) -> list[dict]:
    """Re-read today..frontier to catch additions to already-published days."""
    fresh: list[dict] = []
    today = date.today()
    for target in W.TARGETS:
        key = target["key"]
        if days_ahead is not None:
            last = today + timedelta(days=days_ahead)
        else:
            last = max(frontier_for(state, key), today)
        day = today
        while day <= last and not _stop:
            got = probe_day(target, day)
            if got:
                fresh.extend(record(state, got))
            time.sleep(W.REQUEST_DELAY)
            day += timedelta(days=1)
    return fresh


def prune(state: dict) -> None:
    today = date.today().isoformat()
    state["showtimes"] = {
        uid: s for uid, s in state["showtimes"].items() if s.get("date", "") >= today
    }


# --- main -------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print notifications instead of sending them")
    ap.add_argument("--once", action="store_true",
                    help="run a single frontier probe and exit")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    state = load_state()

    if not state.get("seeded"):
        log(f"First run - seeding {SEED_DAYS} days (no alerts for what's already up)")
        full_sweep(state, days_ahead=SEED_DAYS)
        # The published schedule may already run past SEED_DAYS. Walk the edge
        # out silently now, or the first live cycle would "discover" a fortnight
        # of existing dates and fire a wall of alerts.
        for _ in range(6):
            if _stop or not frontier_probe(state, args.dry_run):
                break
        state["seeded"] = True
        prune(state)
        save_state(state)
        edges = ", ".join(
            f"{t['name'].split(' (')[0]} -> {state['frontier'].get(t['key'], 'none')}"
            for t in W.TARGETS
        )
        log(f"Seeded {len(state['showtimes'])} showtime(s). Frontier: {edges}")
        W.notify(
            "👁️ Odyssey watcher armed",
            f"Tracking {len(state['showtimes'])} IMAX 70mm showtime(s).\n"
            f"Schedule currently runs to:\n{edges}\n"
            "You'll get a ping the moment new dates go up.",
            priority=3, tags=["eye"], dry_run=args.dry_run,
        )

    log(f"Watching. Frontier probe every {FRONTIER_INTERVAL}s, "
        f"full sweep every {FULL_SWEEP_INTERVAL // 60}min. Ctrl+C to stop.")

    last_full = time.monotonic()
    last_beat = time.monotonic()
    fail_streak = 0

    while not _stop:
        cycle_start = time.monotonic()
        try:
            fresh = frontier_probe(state, args.dry_run)

            if time.monotonic() - last_full >= FULL_SWEEP_INTERVAL and not _stop:
                log("Full sweep...")
                fresh += full_sweep(state)
                last_full = time.monotonic()
                prune(state)

            if fresh:
                # Dedupe by uid in case both loops caught the same showtime.
                seen, unique = set(), []
                for s in fresh:
                    tag = (s["theater_key"], s["date"], s["time"])
                    if tag not in seen:
                        seen.add(tag)
                        unique.append(s)
                log(f"*** {len(unique)} NEW showtime(s) - notifying")
                W.group_and_send(unique, "new", args.dry_run)

            save_state(state)
            fail_streak = 0

        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            fail_streak += 1
            log(f"!! cycle failed ({fail_streak}): {exc}")
            if fail_streak == 5:
                W.notify(
                    "⚠️ Odyssey watcher is struggling",
                    f"5 consecutive failed cycles: {exc}",
                    priority=4, tags=["warning"], dry_run=args.dry_run,
                )

        if args.once:
            break

        if HEARTBEAT_HOURS and time.monotonic() - last_beat >= HEARTBEAT_HOURS * 3600:
            last_beat = time.monotonic()
            edges = ", ".join(
                f"{t['key']}->{state['frontier'].get(t['key'], 'none')}"
                for t in W.TARGETS
            )
            W.notify("💤 Odyssey watcher alive",
                     f"Still running. Schedule edge: {edges}",
                     priority=1, tags=["zzz"], dry_run=args.dry_run)

        # Back off hard if we're failing; otherwise jitter so we're not a
        # perfectly periodic signature.
        delay = FRONTIER_INTERVAL * (2 ** min(fail_streak, 4))
        delay += random.uniform(0, FRONTIER_INTERVAL * 0.2)
        elapsed = time.monotonic() - cycle_start
        for _ in range(int(max(0.0, delay - elapsed))):
            if _stop:
                break
            time.sleep(1)

    save_state(state)
    log("State saved. Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
