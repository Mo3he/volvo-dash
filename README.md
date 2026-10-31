# Volvo Dash

A self-hosted log for a Volvo EV (built for the EX40): battery and status at a
glance, plus a record of every trip and charging session with distance,
energy, cost and consumption.

It uses Volvo's official developer API, with your own API application and your
Volvo ID. No reverse-engineered libraries.

> **Status:** early. Running against a real 2027 EX40 since October 2026:
> connection, status, charging and MQTT work. Trip detection has so far only
> been tested against the built-in simulator, which speaks the same JSON as
> Volvo's API. See [First week with the real car](#first-week-with-the-real-car).

## What it does

- **Overview**: battery, range, charge limit, plug and lock state, position,
  door/window/tyre/service checks, and how old the car's last report is.
- **Trips**: distance, duration, energy, consumption, outside temperature,
  start and end address, map, notes, CSV export.
- **Charging**: energy, AC/DC, battery level before and after, power curve,
  cost from per-place prices (editable per session), CSV export.
- **Stats**: distance, energy charged (AC vs DC), cost, and consumption by
  outside temperature, over any period.
- **Controls**: lock, start climate, stop climate.
- **Notifications**: a webhook (ntfy, Discord, Slack) for trip and charging
  events, and for when the Volvo sign-in lapses.

What it deliberately does not do:

- **Vampire drain.** The car does not report while it sleeps, so there is
  nothing to measure it with.
- **Start or stop charging.** Volvo's API has no command for it.
- **Per-trip consumption on short trips.** The car reports battery level in
  whole percent (about 0.8 kWh a step), so trips under 5 km or 2% show "too
  short to measure". Averages use total energy over total distance, where
  that rounding cancels out.

## Home Assistant (MQTT)

Settings → **MQTT and Home Assistant**: enter your broker (for example the
Mosquitto add-on in Home Assistant, with a Home Assistant user's login). The
car then appears in Home Assistant by itself through MQTT discovery, as one
device with battery, range, charging, odometer, lock/door/window state,
last trip and charge, a map tracker, and buttons for lock, climate start/stop
and "check now". Units are metric; Home Assistant converts to yours.

Topics, with `<id>` = `volvodash_` + the last six characters of the VIN:

| Topic | Content |
|---|---|
| `volvodash/<id>/state` | JSON with every value (retained) |
| `volvodash/<id>/location` | `latitude`, `longitude` for the tracker (retained) |
| `volvodash/<id>/event` | one JSON message per trip, charge, plug or lock event |
| `volvodash/<id>/status` | `online` / `offline` (retained; offline is the last will) |
| `volvodash/<id>/command` | publish `lock`, `climatization-start`, `climatization-stop` or `refresh` |

Unlock is deliberately not available over MQTT.

## Run it

With Docker, building from this repository:

```sh
git clone https://github.com/Mo3he/volvo-dash.git && cd volvo-dash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Then open http://localhost:8000. On first visit you choose a dashboard
password. To update: `git pull`, then the same command again.

> A prebuilt image (`ghcr.io/mo3he/volvo-dash`, amd64 and arm64) is set up
> to publish from GitHub Actions but has not been published yet. Once it is,
> `docker-compose.yml` on its own will be enough: download it and run
> `docker compose up -d`.

From source (Python 3.11+ and Node 20+):

```sh
./dev.sh sim    # simulated car with a month of history, no account needed
./dev.sh        # the real thing
```

To deploy a checkout to another machine over SSH, put `DEPLOY_HOST=user@host`
in a `deploy.env` file (git-ignored) and run `./deploy.sh`. It copies the code
and builds there; the data folder on that machine is never touched.

All data lives in one SQLite file, `data/volvodash.db`. Back that up and you
have everything. It contains your API credentials and location history.

If you put this on the internet, put it behind HTTPS (Caddy, Tailscale, a
Cloudflare tunnel). The dashboard password is the only thing in front of it.

## Connect the car

1. Sign in at <https://developer.volvocars.com/account/> with your Volvo ID
   and create an API application. Copy its **VCC API key (primary)**.
2. **Publish** the application. Tick all the scopes listed on the Settings
   page, and set the redirect URI to the one Settings shows you
   (`https://your-host/api/volvo/callback`). Copy the **client ID** and
   **client secret**.
3. In Volvo Dash, open **Settings**, paste the three values, and choose
   **Save and sign in with Volvo ID**.

Two things to know:

- **The redirect URI.** It must match what you registered exactly. If Volvo
  will not accept your dashboard's address (for example plain `http://` on a
  home network), register any address it does accept, enter the same one in
  Settings, sign in, and paste the address you land on into the box under
  "Signed in, but landed on a page that would not load?".
- **The sign-in lasts up to six months.** Volvo's documentation says a grant
  that keeps being refreshed lives for at most six months; after that, or if
  it is revoked, the dashboard shows a banner, the webhook fires, and signing
  in again takes a few seconds. Nothing is logged in between.
- **Run one copy only.** Volvo's refresh tokens are single-use, so two copies
  of the dashboard on the same sign-in will break each other.

## How it works

Volvo's API is not live. The car reports when it drives, charges or wakes,
and every value carries the time the car measured it. The design follows
from that:

- **Adaptive polling.** Four cheap calls every 5 minutes while parked, every
  minute while driving or charging, every 2 minutes while plugged in and
  waiting. Doors, tyres and warnings are on slow timers. A typical day uses
  about 2,000 of Volvo's 10,000 daily calls; polling slows down at 85% and
  stops at 97%.
- **A raw log first.** Every answer from Volvo's API is kept in
  `raw_responses` (compressed, and only when it differs from the previous
  answer for that endpoint: about 100 KB a day). Everything else is derived
  from it: `observations` (the readings detection uses), then trips and
  charging sessions (`backend/volvodash/detect.py`).
- **Rebuildable.** **Settings → Rebuild** re-derives readings, trips and
  charges from the raw log. When detection improves, or the app learns to
  use a field it ignored before, old data improves too. Notes and costs you
  entered are carried over. **Settings → Download raw log** exports it all.
- **Backed up.** A copy of the database goes into `data/backups` daily (last
  14 kept) and before every Rebuild. They sit on the same disk, so copy that
  folder elsewhere if you want protection from losing the disk itself.
- **Car time over poll time.** A trip's end is the moment the car says it
  stopped, not the moment the poller noticed.

Charging energy is the car's reported power added up over the session when
there are enough readings, otherwise battery-level rise times battery size.
Both are stored and shown.

## First week with the real car

The simulator covers two ways the car might behave: reporting every minute
while in use (`VOLVODASH_SIM=live`), or only when something ends
(`VOLVODASH_SIM=sparse`). Which one the EX40 actually does, I don't know yet.
Things to check once it is connected:

- Do trips appear with the right start and end times? A `~` before the start
  time means the car only reported after the trip, and the start is an estimate.
- Do trip maps show a route, or only start and end? That depends on whether
  the car reports position while moving.
- Does a charging session show a power curve and "from charging power"
  energy? If not, the car reports power too rarely and energy falls back to
  the battery-level estimate.
- Is "Usable battery" right? Volvo may report gross capacity. Set the usable
  figure in Settings, then Rebuild.
- If Volvo's charging power turns out to be measured at the wall, "from
  charging power" will be about 10% above "from battery level" on AC. That
  is charging loss, and the wall figure is the right one for cost.

If detection gets something wrong, the raw log still has it: fix the rule in
`detect.py`, add a case to the tests, and Rebuild.

## Development

```sh
cd backend && .venv/bin/pytest     # simulated car end to end, both reporting modes
cd frontend && npm run dev         # hot reload on :5173, proxies /api to :8000
```

| Variable | Meaning |
|---|---|
| `VOLVODASH_DATA` | Data directory (default `./data`) |
| `VOLVODASH_SIM` | `live` or `sparse`: run against the simulator, in a separate `sim.db` |
| `VOLVODASH_SIM_DAYS` | Days of history to simulate at startup (default 30) |
| `VOLVODASH_SIM_SPEED` | How much faster than real time the simulator runs (default 20) |
| `VOLVODASH_NO_AUTH` | `1` disables the dashboard password. Local use only |
| `PORT`, `HOST` | Where to listen (default `0.0.0.0:8000`) |

Layout:

```
backend/volvodash/
  volvo.py      Volvo API client and OAuth
  collector.py  adaptive polling
  detect.py     observations -> trips and charges, and rebuild
  enrich.py     addresses, temperature, webhooks
  sim.py        simulated car and fake Volvo API
  app.py        HTTP API and static files
  db.py         SQLite schema and migrations
frontend/src/   React app (Vite, Recharts, Leaflet)
```
