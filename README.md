# Odyssey IMAX 70mm watcher

Polls Fandango every ~10 minutes for **The Odyssey** in **IMAX 70mm** at:

| Theater | Fandango ID |
|---|---|
| AMC Metreon 16, San Francisco | `aanem` |
| Regal Hacienda Crossings ScreenX/IMAX/RPX, Dublin CA | `aaopk` |

Pushes an [ntfy.sh](https://ntfy.sh) notification, with a tap-through straight to
the Fandango booking page, when either of these happens:

- 🎟️ **New showing listed** — a showtime id appears that we've never seen.
- 🔁 **Seats freed up** — a showtime we'd recorded as `soldout` flips back to
  `available`. For a sold-out run this is usually the one that actually gets you
  in, so it's alerted at the same urgency.

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
Each showtime carries a stable integer `id`, used as the dedup key, a `type` of
`available` / `soldout` / `pastshowtime`, and a `ticketingJumpPageURL` that deep
links to checkout.

State lives in `state.json`, committed back to the repo by the workflow so the
diff survives between runs.

## Setup

1. **Create the repo — make it public.** See the cost note below.

   ```bash
   gh repo create odyssey-watch --public --source=. --push
   ```

2. **Pick an ntfy topic.** It's a shared secret: anyone who guesses the name can
   read your alerts, so use something unguessable.

   ```bash
   # e.g. odyssey-7f3a91c2e4
   ```

3. **Install ntfy** on your phone ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) and
   subscribe to that topic. Allow notifications and, on iOS, make sure the app
   isn't throttled in the background.

4. **Add the topic as a repo secret:**

   ```bash
   gh secret set NTFY_TOPIC --body "odyssey-7f3a91c2e4"
   ```

5. **Kick off the first run** from the Actions tab (or `gh workflow run
   "Odyssey IMAX 70mm watch"`). The first run seeds `state.json` and sends a
   single "watcher armed" message rather than alerting on every existing
   showtime. After that it only tells you about changes.

## Cost — use a public repo

GitHub Actions minutes are free and unlimited on **public** repos. On a private
repo this workflow would run ~144 times/day at 1–2 min each, roughly
**6,000–9,000 minutes/month** against a 2,000-minute free allowance — so it
would start costing real money within days. Nothing here is sensitive except the
ntfy topic, which is a secret, so public is the right call.

## Tuning

Repo **variables** (`gh variable set NAME --body VALUE`):

| Variable | Default | Meaning |
|---|---|---|
| `DAYS_AHEAD` | `21` | How many days forward to sweep |
| `NTFY_SERVER` | `https://ntfy.sh` | Self-hosted ntfy base URL |

Optional secret `NTFY_TOKEN` sets a `Bearer` token for protected ntfy topics.

Cadence is the `cron:` line in `.github/workflows/watch.yml`. Widening
`DAYS_AHEAD` or tightening the cron multiplies request volume — 21 days is
42 requests per sweep, which is already ~6,000 requests/day at 10-minute
intervals. Pushing much past that raises the odds of Fandango rate-limiting the
runner.

## Caveats worth knowing

- **Scheduled runs are not punctual.** GitHub routinely delays cron workflows,
  often by 10–20 minutes at peak, and silently skips them under heavy load. A
  10-minute cron is a best-effort target, not a floor. If you need
  seconds-level reaction time for an on-sale drop, this isn't the tool.
- **Fandango could block the runner.** GitHub's IP ranges are datacenter
  addresses, and the endpoint is undocumented and unsupported — it may start
  403ing or change shape without notice. The script detects a sweep where every
  request failed, keeps the old state instead of treating it as "everything
  vanished", and pings you with a ⚠️ *watcher is blind* alert after 3
  consecutive total failures. If you get that, check the Actions log.
- **Sold-out detection is listing-level**, not seat-level. Fandango marks the
  whole showtime `soldout`; it can't see a single returned seat while the
  showtime still reads as available.
- Scheduled workflows get auto-disabled after 60 days of repo inactivity, but
  this one commits `state.json` regularly, so it stays alive on its own.

## Local testing

```bash
# Fast sanity check, sends nothing, writes nothing
DAYS_AHEAD=5 python watch.py --dry-run

# Rebuild state without alerting
python watch.py --reseed
```

No dependencies — standard library only.
