import { useMemo, useState } from 'react'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, useLive, type Status } from '../api'
import { axis, Battery, Empty, Link, MapView, Tile, Tip, split, useAction } from '../components'
import { ago, dateTime, day, dist, duration, efficiency, kw, kwh, money, now, pct, time } from '../format'

const WORDS: Record<string, string> = {
  IDLE: 'Not charging',
  SCHEDULED: 'Waiting for scheduled start',
  DONE: 'Charging finished',
  ERROR: 'Charging error',
  DISCHARGING: 'Discharging',
}

function headline(s: Status): string {
  const o = s.obs
  if (!o) return 'Waiting for the first reading from the car'
  if (s.trip) return `Driving since ${time(s.trip.started_at)}`
  if (s.charge) {
    const place = s.charge.place_name ? ` at ${s.charge.place_name}` : ''
    return `Charging${place}`
  }
  if (o.connection === 'CONNECTED') return `Plugged in. ${WORDS[o.charging_status] ?? 'Not charging'}`
  return 'Parked'
}

/** The car's own most recent timestamp: how old what we show really is. */
function carTime(o: Record<string, any>): number | null {
  const stamps = [o.soc_ts, o.charge_ts, o.engine_ts, o.availability_ts, o.odometer_ts, o.location_ts].filter(Boolean)
  return stamps.length ? Math.max(...stamps) : null
}

export function Overview({ status: s }: { status: Status }) {
  const o = s.obs
  const c = s.connection
  const refresh = useAction()

  if (!o || o.soc == null) {
    return (
      <Empty>
        {c.connected
          ? 'Connected. Waiting for the first reading from the car.'
          : 'Once the car is connected, its battery, trips and charging sessions will appear here.'}
      </Empty>
    )
  }

  const reported = carTime(o)
  const lock = s.state.centralLock?.value as string | undefined
  const remaining = s.charge && o.mins_to_target ? o.mins_to_target * 60 : null

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{headline(s)}</h1>
          <p className="soft small">
            Car last reported {ago(reported)} · checked {ago(c.last_poll)}
            {c.last_error && <span className="error"> · {c.last_error}</span>}
          </p>
        </div>
        <div className="row">
          {refresh.note}
          <button className="btn" disabled={refresh.busy || !c.connected} onClick={() => refresh.run(() => api('/refresh', 'POST'))}>
            {refresh.busy ? 'Checking…' : 'Check now'}
          </button>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card hero">
          <div className="row" style={{ alignItems: 'flex-end', justifyContent: 'space-between' }}>
            <div className="hero-figure">
              {Math.round(o.soc)}
              <small>%</small>
            </div>
            <div className="right">
              <div style={{ fontSize: 20, fontWeight: 600 }}>{dist(o.range_km, 0)}</div>
              <div className="muted small">estimated range</div>
            </div>
          </div>
          <Battery soc={o.soc} target={o.target_soc} />
          {s.charge ? (
            <div className="facts">
              <div><dt>Power</dt><dd>{kw(o.power_w != null ? o.power_w / 1000 : null)}</dd></div>
              <div><dt>Added so far</dt><dd>{kwh(s.charge.energy_kwh)}</dd></div>
              <div><dt>Since</dt><dd>{time(s.charge.started_at)} at {pct(s.charge.soc_start)}</dd></div>
              <div><dt>To {pct(o.target_soc)}</dt><dd>{remaining ? duration(remaining) : '–'}</dd></div>
            </div>
          ) : (
            <div className="facts">
              <div><dt>Charge limit</dt><dd>{pct(o.target_soc)}</dd></div>
              <div><dt>Cable</dt><dd>{o.connection === 'CONNECTED' ? 'Plugged in' : 'Unplugged'}</dd></div>
              <div><dt>Doors</dt><dd>{lock === 'LOCKED' ? 'Locked' : lock === 'UNLOCKED' ? 'Unlocked' : '–'}</dd></div>
              <div><dt>Odometer</dt><dd>{dist(o.odometer_km, 0)}</dd></div>
            </div>
          )}
        </div>

        <div className="grid" style={{ alignContent: 'start' }}>
          <div className="grid tiles">
            <LastTrip s={s} />
            <LastCharge s={s} />
          </div>
          <Controls s={s} />
        </div>
      </div>

      <SocHistory />

      <div className="grid cols-2">
        <div className="card">
          <h2>Where it is</h2>
          {o.lat != null ? (
            <>
              <MapView markers={[{ at: [o.lat, o.lon], label: 'Car', end: true }]} />
              <p className="hint" style={{ marginTop: 8 }}>Position reported {ago(o.location_ts)}.</p>
            </>
          ) : (
            <Empty>
              {c.unavailable.includes('location')
                ? 'Volvo does not share this car’s position with the API.'
                : 'No position reported yet.'}
            </Empty>
          )}
        </div>
        <div className="grid" style={{ alignContent: 'start' }}>
          <Checks s={s} />
          <Activity />
        </div>
      </div>
    </>
  )
}

function LastTrip({ s }: { s: Status }) {
  const t = s.last_trip
  if (!t) return <Tile label="Last trip" value="–" sub="No trips yet" />
  const d = split(dist(t.distance_km))
  return (
    <Tile
      label="Last trip"
      value={d.value}
      unit={d.unit}
      sub={<>{day(t.ended_at)}, {time(t.ended_at)} · {t.efficiency ? efficiency(t.efficiency) : duration(t.ended_at - t.started_at)}</>}
    />
  )
}

function LastCharge({ s }: { s: Status }) {
  const c = s.last_charge
  if (!c) return <Tile label="Last charge" value="–" sub="No sessions yet" />
  const e = split(kwh(c.energy_billed_kwh))
  return (
    <Tile
      label="Last charge"
      value={e.value}
      unit={e.unit}
      sub={<>{day(c.started_at)} · {pct(c.soc_start)} → {pct(c.soc_end)}{c.cost != null && <> · {money(c.cost)}</>}</>}
    />
  )
}

function Controls({ s }: { s: Status }) {
  const { busy, run, note } = useAction()
  const can = (name: string) => s.connection.commands.includes(name)
  if (!['LOCK', 'CLIMATIZATION_START', 'CLIMATIZATION_STOP'].some(can)) return null

  const send = (name: string, done: string) =>
    run(async () => {
      const r = await api<{ status: string; message: string }>(`/command/${name}`, 'POST')
      return ['COMPLETED', 'DELIVERED', 'RUNNING', 'WAITING'].includes(r.status)
        ? done
        : `The car answered ${r.status.toLowerCase().replace(/_/g, ' ')}. ${r.message ?? ''}`
    })

  return (
    <div className="card">
      <h2>Controls</h2>
      <div className="row">
        {can('CLIMATIZATION_START') && (
          <button className="btn" disabled={busy} onClick={() => send('climatization-start', 'Climate start sent.')}>Start climate</button>
        )}
        {can('CLIMATIZATION_STOP') && (
          <button className="btn" disabled={busy} onClick={() => send('climatization-stop', 'Climate stop sent.')}>Stop climate</button>
        )}
        {can('LOCK') && (
          <button className="btn" disabled={busy} onClick={() => send('lock', 'Lock sent.')}>Lock</button>
        )}
      </div>
      <p className="hint" style={{ marginTop: 10 }}>
        {note ?? 'Volvo’s API cannot start or stop charging, and does not report whether climate is running.'}
      </p>
    </div>
  )
}

const OPEN_LABELS: Record<string, string> = {
  frontLeftDoor: 'Front left door', frontRightDoor: 'Front right door',
  rearLeftDoor: 'Rear left door', rearRightDoor: 'Rear right door',
  hood: 'Bonnet', tailgate: 'Tailgate',
  frontLeftWindow: 'Front left window', frontRightWindow: 'Front right window',
  rearLeftWindow: 'Rear left window', rearRightWindow: 'Rear right window', sunroof: 'Sunroof',
}
const TYRES: Record<string, string> = {
  frontLeft: 'front left', frontRight: 'front right', rearLeft: 'rear left', rearRight: 'rear right',
}

function Checks({ s }: { s: Status }) {
  const st = s.state
  const open = Object.keys(OPEN_LABELS).filter((k) => st[k] && ['OPEN', 'AJAR'].includes(st[k].value))
  const tyres = Object.keys(TYRES).filter((k) => st[k] && !['NO_WARNING', 'UNSPECIFIED'].includes(st[k].value))
  // Some cars answer UNSPECIFIED for every tyre: that means "not reported", not "fine".
  const tyresReported = Object.keys(TYRES).some((k) => st[k] && st[k].value !== 'UNSPECIFIED')
  const warnings = Object.keys(st).filter(
    (k) => k.endsWith('Warning') && !['NO_WARNING', 'UNSPECIFIED'].includes(st[k].value),
  )
  const service = st.distanceToService
  const months = st.timeToService
  const row = (label: string, ok: boolean, text: string) => (
    <>
      <span className="row" style={{ gap: 8 }}><span className={`dot ${ok ? 'good' : 'warn'}`} />{label}</span>
      <span>{text}</span>
    </>
  )
  return (
    <div className="card">
      <h2>Checks</h2>
      <div className="kv">
        {st.frontLeftDoor && row('Doors and windows', !open.length, open.length ? `${open.map((k) => OPEN_LABELS[k]).join(', ')} open` : 'All closed')}
        {tyresReported && row('Tyre pressure', !tyres.length, tyres.length ? `Check ${tyres.map((k) => TYRES[k]).join(', ')}` : 'No warnings')}
        {row('Warnings', !warnings.length, warnings.length ? warnings.map((w) => w.replace(/Warning$/, '').replace(/([A-Z])/g, ' $1').toLowerCase()).join(', ') : 'None')}
        {service && row('Next service', true, `${dist(service.value, 0)}${months ? ` or ${months.value} ${months.unit ?? 'months'}` : ''}`)}
      </div>
    </div>
  )
}

const EVENT_TEXT: Record<string, (e: any) => string> = {
  trip_started: () => 'Trip started',
  trip_ended: (e) => `Trip ended, ${dist(e.distance_km)}`,
  charge_started: (e) => `Charging started at ${pct(e.soc)}`,
  charge_ended: (e) => `Charging ended at ${pct(e.soc)}${e.energy_kwh ? `, ${kwh(e.energy_kwh)}` : ''}`,
  plugged: () => 'Plugged in',
  unplugged: () => 'Unplugged',
  locked: () => 'Locked',
  unlocked: () => 'Unlocked',
  needs_reauth: () => 'Volvo sign-in expired',
  command: (e) => `Sent ${String(e.command).replace(/-/g, ' ')}`,
}

function Activity() {
  const { data } = useLive<any[]>('/events?limit=8')
  return (
    <div className="card">
      <h2>Recent activity</h2>
      {data?.length ? (
        <div className="feed">
          {data.map((e) => (
            <div key={e.id}>
              <span>{EVENT_TEXT[e.type]?.(e) ?? e.type}</span>
              <span className="muted nowrap">{dateTime(e.ts)}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="muted">Nothing yet.</p>
      )}
    </div>
  )
}

const SPANS = [['24 h', 1], ['7 days', 7], ['30 days', 30]] as const

function SocHistory() {
  const [days, setDays] = useState<number>(7)
  const from = useMemo(() => Math.floor((now() - days * 86400) / 600) * 600, [days])
  const { data } = useLive<{ t: number; soc: number; range_km: number | null }[]>(`/history?from=${from}`)
  const end = now()
  const points = useMemo(() => {
    if (!data?.length) return []
    const rows = data.map((d) => ({ ...d, t: Math.max(d.t, from) }))
    // Hold the last value to "now": the battery did not vanish, the car is just quiet.
    return [...rows, { ...rows[rows.length - 1], t: end }]
  }, [data, from]) // eslint-disable-line react-hooks/exhaustive-deps

  const ticks = useMemo(() => {
    const step = days === 1 ? 6 * 3600 : days === 7 ? 86400 : 5 * 86400
    const out = []
    for (let t = Math.ceil(from / step) * step; t < end; t += step) out.push(t)
    return out
  }, [from, days]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="card">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <h2>Battery level</h2>
        <div className="seg" role="group" aria-label="Time span">
          {SPANS.map(([label, d]) => (
            <button key={d} className={d === days ? 'on' : ''} aria-pressed={d === days} onClick={() => setDays(d)}>{label}</button>
          ))}
        </div>
      </div>
      {points.length ? (
        <div className="chart" style={{ height: 220 }}>
          <ResponsiveContainer>
            <AreaChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: -18 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis
                dataKey="t" type="number" domain={[from, end]} ticks={ticks} {...axis}
                tickFormatter={(t) => (days === 1 ? time(t) : day(t).split(',')[0])}
              />
              <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} {...axis} axisLine={false} tickFormatter={(v) => `${v}%`} />
              <Tooltip
                cursor={{ stroke: 'var(--axis)' }}
                content={<Tip title={(d) => dateTime(d.t)} rows={(d) => [
                  { label: 'Battery', value: pct(d.soc), color: 'var(--series-1)' },
                  { label: 'Range', value: dist(d.range_km, 0) },
                ]} />}
              />
              <Area
                type="stepAfter" dataKey="soc" stroke="var(--series-1)" strokeWidth={2}
                fill="var(--series-1)" fillOpacity={0.1} isAnimationActive={false}
                activeDot={{ r: 4, stroke: 'var(--surface)', strokeWidth: 2 }}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <Empty>No readings in this period.</Empty>
      )}
      <p className="hint" style={{ marginTop: 8 }}>
        The car reports when it drives, charges or wakes, not on a schedule, so flat stretches are simply the last value it sent.
        See <Link to="/trips">Trips</Link> and <Link to="/charging">Charging</Link> for what happened in between.
      </p>
    </div>
  )
}
