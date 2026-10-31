# Later

## Car commands: honk/flash and unlock

Volvo's API supports unlock, honk, flash and honk-and-flash, and the car lists
them as available. They were left out on purpose (Oct 2026). To add them:

- **Scopes.** Add `conve:honk_flash` and `conve:unlock` to `SCOPES` in
  `backend/volvodash/volvo.py`, and to `OPTIONAL_SCOPES` so a refusal falls
  back. Needs a new Volvo sign-in (paste flow), and the application on the
  developer portal must include them.
- **Honk/flash.** Harmless and useful for finding the car. Add to `COMMANDS`
  in `volvo.py`, the dashboard Controls card, and as MQTT buttons in
  `backend/volvodash/mqtt.py`.
- **Unlock.** Security-sensitive. Dashboard only, behind a confirmation. In
  Home Assistant only as a lock entity, and only if a separate "allow unlock
  over MQTT" setting is switched on (off by default).
- **Wording.** The Overview's Controls hint says the API "cannot start or stop
  charging", which reads as "no control at all". Say what the buttons do.

Deploy when the car is not charging: a deploy restarts the container.

## Trip energy from the car's own consumption figure

Per-trip energy now comes from the battery percentage, which moves in whole
percent (about 0.8 kWh on this car), so short trips are rough.

The statistics endpoint has `averageEnergyConsumption` (kWh/100 km), which is
averaged over the manual trip meter `tripMeterManual`. Their product is the
energy the car measured since that meter was last reset, so the difference
across a trip is the trip's energy, accurate to a few hundredths of a kWh.
First real trip (31 Oct 2026, 9.8 km): 27.9 x 26.2 = 7.31 kWh before,
26.2 x 36.0 = 9.43 kWh after, so 2.12 kWh, against 1.6 +/- 0.8 kWh from
battery percentage.
Second trip (31 Oct 2026, 10.0 km, no preconditioning): 26.1 x 36.0 = 9.40
kWh before, 24.0 x 46.0 = 11.04 kWh after, so 1.64 kWh, against 1.63 kWh
from battery percentage (96% to 94%).

Unlike the trip meters, the average updates about every minute while
driving, so it could also show live consumption during a trip.

Open questions before relying on it:
- Use the readings from just before the trip starts: the average already
  moved while parked (27.5 to 27.9 during preconditioning), so climate while
  parked is counted too.
- Breaks when the manual trip meter is reset; detect a drop and fall back.
- Confirm over a few more trips that it agrees with battery percentage on
  average.

Everything needed is already in the raw log, so it can be added later and
applied to past trips with Rebuild. Both fields need columns in
`observations` (via `fold.py`).
