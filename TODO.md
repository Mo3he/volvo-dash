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
