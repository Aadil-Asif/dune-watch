# Odyssey IMAX 70mm watcher

Polls Fandango every ~10 minutes for **The Odyssey** in **IMAX 70mm** at:

| Theater | Fandango ID |
|---|---|
| AMC Metreon 16, San Francisco | `aanem` |
| Regal Hacienda Crossings ScreenX/IMAX/RPX, Dublin CA | `aaopk` |

Pushes an [ntfy.sh](https://ntfy.sh) notification, with a tap-through straight to
the Fandango booking page, when **new dates go up**.

Two watchers, on purpose:

| | Where | Cadence | Role |
|---|---|---|---|
| `daemon.py` | your always-on Windows box | ~90 s | **primary** |
| `watch.py` via Actions | GitHub | 30 min | fallback, alerts tagged `[backup]` |

The fallback exists because the daemon dies with your power or your ISP. It runs
from a different network and pings the same ntfy topic at lower priority, so a
`[backup]` alert is also a signal that your local watcher has stopped.

> **Freed seats are not alerted.** Fandango exposes a per-showtime
> `available` / `soldout` flag, and the code can diff it, but it's off by
> default (`ALERT_FREED=1` to enable). A showtime flipping back to `available`
> is usually one returned seat, which is no use when you're booking for a
> group.

## How it works

Fandango's own site loads showtimes client-side from an internal endpoint:

```
GET https://www.fandango.com/napi/theaterMovieShowtimes/{theaterId}?startDate=YYYY-MM-DD
```

It returns `403 {"error":"FORBIDDEN"}` unless you send a `Referer` header
pointing at that theater's page. No cookie, token, or login is needed — just the
referer and a browser `User-Agent`. There's no multi-day parameter, so it's one
request per theater per day.

Within the payload, the real 15/70 film screenings are the showtimes whose
`filmFormat[].filterName` is `IMAX 70MM` (the `filmFormatHeader` field just says
"Premium Format", and a plain `IMAX` tag means digital IMAX — not what you want).
Each showtime carries a `type` of `available` / `soldout` / `pastshowtime` and a
`ticketingJumpPageURL` that deep links to checkout.

**Dedup is keyed on `ticketingDate`, not `id`.** The obvious key is the integer
`id`, but Fandango returns `id: null` for some listings — at time of writing,
every one of Metreon's 70mm showtimes — and has been observed flipping the same
showtime between a real id and null within an hour. Keying on `id` silently
dropped the entire SF theatre, and would also have re-announced showtimes as
"new" each time the id came back. `ticketingDate` (`2026-08-15+10:00`) is stable
and unique per theatre slot.

### Frontier polling

Theatres publish their schedule forward in time, a batch of days at a time. So
the daemon doesn't re-sweep the whole horizon every cycle — it watches the
**frontier**: the first day past the last day that currently has showtimes.

```
... Aug 17  Aug 18  Aug 19 │ Aug 20  Aug 21
    showtimes listed       │ ← probe here (~4 requests/cycle)
                     frontier
```

When a probe lands, it walks forward until days come back empty again, so a
whole newly-published week is caught in one cycle. It probes 2 days past the
edge rather than 1, so a single dark day can't stall the frontier permanently.

A **full sweep** of `today..frontier` still runs every 30 min, because a theatre
can also add a screening to a day it already published — the frontier probe
would never see that.

The upshot: ~4 requests every 90 s, which is *less* traffic than the old
21-day sweep at 10-minute intervals, while reacting roughly 10× faster.

State lives in `state.json` (Actions, committed to the repo) and
`daemon_state.json` (local, gitignored). They're independent, which is what
makes the fallback a genuine fallback.

## Setup — always-on Windows box (primary)

```cmd
cd C:\Users\Sam\Documents\odyssey-watch
echo odyssey70-cec552047d> ntfy_topic.txt
run-watcher.cmd
```

Leave the window open. `run-watcher.cmd` restarts the daemon if it crashes;
closing the window stops it. `ntfy_topic.txt` is gitignored so the topic can't
leak into the public repo.

First launch seeds ~30 days, silently walks the frontier out to the true edge,
and sends a single **👁️ watcher armed** message telling you how far the
schedule currently runs. Every subsequent start sends **▶️ watcher started**
with the same summary, so relaunching after a reboot is visibly confirmed.
After that it's quiet until dates actually move.

To survive reboots without logging in, register it with Task Scheduler:

```cmd
schtasks /create /tn "Odyssey watcher" /tr "%CD%\run-watcher.cmd" /sc onstart /rl highest
```

### Daemon tuning

| Env var | Default | Meaning |
|---|---|---|
| `FRONTIER_INTERVAL` | `90` | Seconds between frontier probes |
| `FULL_SWEEP_INTERVAL` | `1800` | Seconds between full sweeps |
| `PROBE_LOOKAHEAD` | `2` | Days probed past the frontier |
| `SEED_DAYS` | `30` | Horizon for the initial seed |
| `MIN_SWEEP_DAYS` | `30` | Full sweep always covers at least this far out |
| `STARTUP_PING` | `1` | Ping on every start; `0` disables |
| `HEARTBEAT_HOURS` | `24` | Low-priority "still alive" ping; `0` disables |
| `ALERT_FREED` | `0` | Set `1` to also alert on soldout → available |

```cmd
python daemon.py --once --dry-run    REM single cycle, sends nothing
```

## Setup — GitHub Actions (fallback)

Already live at [sgzasher/odyssey-watch](https://github.com/sgzasher/odyssey-watch)
with `NTFY_TOPIC` set as a repo secret. It seeds `state.json` on its first run
and sends one "watcher armed" message rather than alerting on everything already
listed.

Install ntfy on your phone ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)), subscribe
to the topic, and make sure background refresh isn't throttled.

The repo is **public** deliberately: Actions minutes are free and unlimited on
public repos, whereas on a private repo even the half-hourly fallback runs
~2,200 min/month against a 2,000-minute allowance. The only secret is the ntfy
topic, and that's a repo secret, not in the tree.

Fallback tuning uses repo **variables** (`gh variable set NAME --body VALUE`):
`DAYS_AHEAD` (default `21`) and `NTFY_SERVER` (default `https://ntfy.sh`).
Optional secret `NTFY_TOKEN` sets a `Bearer` token for protected ntfy topics.

## Caveats worth knowing

- **The daemon is only as alive as its window.** If the box sleeps, the process
  is killed, or Python crashes in a way the `.cmd` loop can't recover from, you
  are relying on the half-hourly `[backup]` alerts to notice. That's what the
  daily 💤 heartbeat is for — if it stops arriving, something is wrong.
- **Fandango could rate-limit or change the endpoint.** It's undocumented and
  unsupported; it may start 403ing or change shape without notice. Both watchers
  detect a sweep where every request failed, keep the old state rather than
  treating it as "everything vanished", and send a ⚠️ alert. Your residential IP
  is much less exposed to this than GitHub's datacenter ranges — which is the
  main reason the local daemon is primary.
- **GitHub cron is not punctual.** It routinely runs late by 10–20 minutes at
  peak and skips runs under load. Fine for a fallback; useless as a primary.
- **Sold-out detection is listing-level**, not seat-level — Fandango marks the
  whole showtime `soldout`, so it cannot see one returned seat. Another reason
  `ALERT_FREED` stays off.
- Late-night screenings are listed under the previous day, matching Fandango's
  own presentation: a 2:45a show appears under the preceding evening's date.
- Scheduled workflows get auto-disabled after 60 days of repo inactivity, but
  the fallback commits `state.json` regularly, so it stays alive on its own.

## Local testing

```bash
python daemon.py --once --dry-run          # one daemon cycle, sends nothing
DAYS_AHEAD=5 python watch.py --dry-run     # one fallback-style sweep
python watch.py --reseed                   # rebuild fallback state, no alerts
```

No dependencies — standard library only.
