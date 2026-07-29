#!/usr/bin/env python3
"""Watch Fandango for IMAX 70mm showings of The Odyssey and push ntfy.sh alerts.

Polls Fandango's internal showtimes API (one request per theater per date),
diffs against committed state, and notifies on:

  * NEW      - a showtime id we've never seen before (new date/time listed)
  * FREED    - a showtime we'd recorded as soldout that is now available

Standard library only, so CI runs need no pip install.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

# Emoji in log lines blow up on a cp1252 Windows console otherwise.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - older//odd streams just keep their codec
        pass

# --- what we're hunting -----------------------------------------------------

TARGETS = [
    {
        "key": "metreon",
        "id": "aanem",
        "slug": "amc-metreon-16-aanem",
        "name": "AMC Metreon 16 (SF)",
    },
    {
        "key": "hacienda",
        "id": "aaopk",
        "slug": "regal-hacienda-crossings-screenx-imax-and-rpx-aaopk",
        "name": "Regal Hacienda Crossings (Dublin)",
    },
]

TITLE_MATCH = os.environ.get("TITLE_MATCH", "odyssey").lower()
# Fandango tags each showtime with filmFormat entries; this filterName is the
# one that means true 15/70 film, as opposed to plain digital "IMAX".
FORMAT_MATCH = os.environ.get("FORMAT_MATCH", "IMAX 70MM").upper()

DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "21"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.8"))
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "12"))
FAILURE_ALERT_AFTER = int(os.environ.get("FAILURE_ALERT_AFTER", "3"))

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

# Booking for a group, so a single returned seat is no use -- off by default.
ALERT_FREED = os.environ.get("ALERT_FREED", "0") not in ("0", "", "false", "no")
# Set on the GitHub Actions fallback so its alerts are visibly the backup's and
# don't buzz at the same urgency as the local daemon's.
WATCHER_LABEL = os.environ.get("WATCHER_LABEL", "").strip()

API = "https://www.fandango.com/napi/theaterMovieShowtimes/{tid}?startDate={day}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

STATE_PATH = os.environ.get("STATE_PATH", "state.json")


# --- fetching ---------------------------------------------------------------


def fetch_day(target: dict, day: str, attempts: int = 3) -> dict | None:
    """Return the parsed viewModel for one theater/day, or None if it failed.

    The API 403s without a Referer pointing at that theater's page -- that
    header, not a cookie or token, is what unlocks it.
    """
    url = API.format(tid=target["id"], day=day)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://www.fandango.com/{target['slug']}/theater-page",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))["viewModel"]
        except Exception as exc:  # noqa: BLE001 - any failure is just a retry
            if attempt == attempts - 1:
                print(f"  !! {target['key']} {day}: {exc}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt + random.random())
    return None


def theater_page_url(target: dict, day: str) -> str:
    return (
        f"https://www.fandango.com/{target['slug']}/theater-page"
        f"?format=IMAX+70MM&date={day}"
    )


def extract(view_model: dict, target: dict, day: str) -> dict[str, dict]:
    """Pull matching showtimes out of one day's payload, keyed by uid."""
    found: dict[str, dict] = {}
    for movie in view_model.get("movies") or []:
        if TITLE_MATCH not in (movie.get("title") or "").lower():
            continue
        for variant in movie.get("variants") or []:
            for group in variant.get("amenityGroups") or []:
                for show in group.get("showtimes") or []:
                    formats = {
                        (f.get("filterName") or "").upper()
                        for f in show.get("filmFormat") or []
                    }
                    if FORMAT_MATCH not in formats:
                        continue
                    status = show.get("type") or "unknown"
                    if status == "pastshowtime" or show.get("expired"):
                        continue
                    sid = show.get("id")
                    if sid is None:
                        continue
                    found[f"{target['key']}:{sid}"] = {
                        "theater": target["name"],
                        "theater_key": target["key"],
                        "title": movie.get("title") or "The Odyssey",
                        "date": day,
                        "time": show.get("date") or "?",
                        "status": status,
                        "url": show.get("ticketingJumpPageURL")
                        or theater_page_url(target, day),
                    }
    return found


def sweep() -> tuple[dict[str, dict], set[tuple[str, str]], int]:
    """Poll every theater/day. Returns (showtimes, days_fetched_ok, failures)."""
    current: dict[str, dict] = {}
    fetched_ok: set[tuple[str, str]] = set()
    failures = 0
    today = date.today()
    days = [(today + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]

    for target in TARGETS:
        for day in days:
            vm = fetch_day(target, day)
            if vm is None:
                failures += 1
            else:
                fetched_ok.add((target["key"], day))
                hits = extract(vm, target, day)
                current.update(hits)
                if hits:
                    avail = sum(1 for h in hits.values() if h["status"] == "available")
                    print(
                        f"  {target['key']:9} {day}  {len(hits):2} showtime(s), "
                        f"{avail} available"
                    )
            time.sleep(REQUEST_DELAY)
    return current, fetched_ok, failures


# --- state ------------------------------------------------------------------


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 1, "seeded": False, "consecutive_failures": 0,
                "showtimes": {}}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


# --- notification -----------------------------------------------------------


def notify(title: str, message: str, *, priority: int = 4,
           tags: list[str] | None = None, click: str | None = None,
           dry_run: bool = False) -> None:
    """Publish to ntfy via its JSON API (handles UTF-8; headers would not)."""
    if WATCHER_LABEL:
        title = f"[{WATCHER_LABEL}] {title}"
        priority = min(priority, 3)
    if dry_run or not NTFY_TOPIC:
        print(f"\n--- ntfy ({'dry-run' if dry_run else 'NO TOPIC SET'}) ---")
        print(f"{title}\n{message}\n{click or ''}")
        return
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or [],
    }
    if click:
        payload["click"] = click
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(
        NTFY_SERVER, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"  -> notified: {title}")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! ntfy failed: {exc}", file=sys.stderr)


def pretty_day(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return iso
    # %-d is not portable to Windows, so strip the zero by hand.
    return f"{dt.strftime('%a %b')} {dt.day}"


def time_key(label: str) -> int:
    """Sort '10:00a' / '2:00p' chronologically; unparseable sorts last."""
    try:
        raw = label.strip().lower()
        meridiem = raw[-1]
        hh, mm = raw[:-1].split(":")
        hour = int(hh) % 12 + (12 if meridiem == "p" else 0)
        return hour * 60 + int(mm)
    except Exception:  # noqa: BLE001
        return 10**6


def digest(events: list[dict], dry_run: bool) -> None:
    """A whole week landing at once should be one message, not fifteen."""
    by_theater: dict[str, list[dict]] = {}
    for ev in events:
        by_theater.setdefault(ev["theater"], []).append(ev)

    lines = []
    for theater, shows in sorted(by_theater.items()):
        days = sorted({s["date"] for s in shows})
        span = pretty_day(days[0])
        if len(days) > 1:
            span += f" – {pretty_day(days[-1])}"
        lines.append(f"{theater}\n  {span} · {len(shows)} showtime(s)")

    first = min(events, key=lambda s: (s["date"], time_key(s["time"])))
    by_key = {t["key"]: t for t in TARGETS}
    target = by_key.get(first["theater_key"])
    click = theater_page_url(target, first["date"]) if target else first["url"]

    notify(
        f"🎟️ New IMAX 70mm dates up — {len(events)} showtimes",
        "\n".join(lines) + "\n\nBook now — these go fast.",
        priority=5, tags=["film_projector"], click=click, dry_run=dry_run,
    )


def group_and_send(events: list[dict], kind: str, dry_run: bool) -> int:
    """One message per (theater, date) so the tap-through is a real booking link."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for ev in events:
        buckets.setdefault((ev["theater"], ev["date"]), []).append(ev)

    # Past a certain size, per-day messages stop being useful and start being
    # a notification storm you'd swipe away without reading.
    if kind == "new" and len(buckets) > MAX_MESSAGES:
        digest(events, dry_run)
        return 1

    sent = 0
    for (theater, day), shows in sorted(buckets.items(), key=lambda kv: kv[0][1]):
        if sent >= MAX_MESSAGES:
            print(f"  .. {len(buckets) - sent} more group(s) suppressed")
            break
        shows.sort(key=lambda s: time_key(s["time"]))
        times = ", ".join(dict.fromkeys(s["time"] for s in shows))
        if kind == "new":
            title = f"🎟️ {len(shows)} new IMAX 70mm showing(s) — {theater}"
            tags, priority = ["film_projector"], 5
        else:
            title = f"🔁 {len(shows)} seat(s) freed up — {theater}"
            tags, priority = ["rotating_light"], 5
        notify(
            title,
            f"{pretty_day(day)}: {times}\nBook now — these go fast.",
            priority=priority, tags=tags, click=shows[0]["url"], dry_run=dry_run,
        )
        sent += 1
    return sent


# --- main -------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print notifications instead of sending, don't save state")
    ap.add_argument("--reseed", action="store_true",
                    help="rebuild state from scratch without alerting")
    args = ap.parse_args()

    print(f"Sweeping {len(TARGETS)} theaters x {DAYS_AHEAD} days "
          f"for '{TITLE_MATCH}' in {FORMAT_MATCH} ...")
    current, fetched_ok, failures = sweep()
    total_requests = len(TARGETS) * DAYS_AHEAD
    print(f"Sweep done: {len(current)} matching showtime(s), "
          f"{failures}/{total_requests} request(s) failed")

    state = load_state()
    previous: dict[str, dict] = state.get("showtimes", {})

    # If every single request failed we're blocked or offline -- don't let an
    # empty sweep look like "everything was removed".
    if failures == total_requests:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        print(f"Total sweep failure #{state['consecutive_failures']}", file=sys.stderr)
        if state["consecutive_failures"] >= FAILURE_ALERT_AFTER:
            notify(
                "⚠️ Odyssey watcher is blind",
                f"{state['consecutive_failures']} consecutive failed sweeps. "
                "Fandango may be blocking the runner — check the Actions log.",
                priority=4, tags=["warning"], dry_run=args.dry_run,
            )
            state["consecutive_failures"] = 0
        if not args.dry_run:
            save_state(state)
        return 1
    state["consecutive_failures"] = 0

    first_run = not state.get("seeded") or args.reseed

    new_events: list[dict] = []
    freed_events: list[dict] = []
    if not first_run:
        for uid, show in current.items():
            before = previous.get(uid)
            if before is None:
                new_events.append(show)
            elif before.get("status") == "soldout" and show["status"] == "available":
                freed_events.append(show)

    # Carry forward showtimes on days we couldn't reach, so a transient failure
    # doesn't drop them and re-announce them as "new" next run.
    merged = dict(current)
    today_iso = date.today().isoformat()
    for uid, show in previous.items():
        if uid in merged:
            continue
        if show.get("date", "") < today_iso:
            continue  # past, prune
        if (show.get("theater_key"), show.get("date")) in fetched_ok:
            continue  # genuinely gone from a day we did read
        merged[uid] = show

    state["showtimes"] = merged
    state["seeded"] = True
    state["last_run"] = datetime.now().astimezone().isoformat(timespec="seconds")

    if first_run:
        avail = sum(1 for s in current.values() if s["status"] == "available")
        print("First run — seeding state, no alerts for existing showtimes.")
        notify(
            "👁️ Odyssey watcher armed",
            f"Tracking {len(current)} IMAX 70mm showtime(s) "
            f"({avail} available) across {len(TARGETS)} theaters.\n"
            "You'll get a ping when new ones list or seats free up.",
            priority=3, tags=["eye"], dry_run=args.dry_run,
        )
    else:
        print(f"Diff: {len(new_events)} new, {len(freed_events)} freed"
              f"{'' if ALERT_FREED else ' (freed alerts off)'}")
        if freed_events and ALERT_FREED:
            group_and_send(freed_events, "freed", args.dry_run)
        if new_events:
            group_and_send(new_events, "new", args.dry_run)
        if not new_events and not (freed_events and ALERT_FREED):
            print("No change.")

    if args.dry_run:
        print("\n(dry run — state not written)")
    else:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
