import { useMemo, useState } from 'react'
import { Area, AreaChart, CartesianGrid, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, useLive, type Status } from '../api'
import { axis, BatteryRing, Empty, Icon, Link, MapView, Tile, Tip, split, useAction } from '../components'
import { ago, dateTime, day, dist, distUnit, distValue, duration, efficiency, kw, kwh, money, now, pct, time } from '../format'

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

      <div className={`card hero${s.vehicle.image ? '' : ' no-car'}`}>
        <BatteryRing soc={o.soc} target={o.target_soc}>
          <div className="pct nums">{Math.round(o.soc)}<small>%</small></div>
          <div className="range nums">{dist(o.range_km, 0)}</div>
          <div className="range-label">estimated range</div>
        </BatteryRing>
        <div className="hero-info">
          <div className="chips">
            <span className="chip"><Icon name="plug" />{s.charge ? 'Charging' : o.connection === 'CONNECTED' ? 'Plugged in' : 'Unplugged'}</span>
            {lock && <span className="chip"><Icon name={lock === 'LOCKED' ? 'lock' : 'unlock'} />{lock === 'LOCKED' ? 'Locked' : 'Unlocked'}</span>}
            {o.target_soc != null && <span className="chip"><Icon name="battery" />Limit {pct(o.target_soc)}</span>}
          </div>
          {s.charge ? (
            <div className="facts">
              <div><dt>Power</dt><dd>{kw(o.power_w != null ? o.power_w / 1000 : null)}</dd></div>
              <div><dt>Added so far</dt><dd>{kwh(s.charge.energy_kwh)}</dd></div>
              <div><dt>Since</dt><dd>{time(s.charge.started_at)} at {pct(s.charge.soc_start)}</dd></div>
              <div><dt>To {pct(o.target_soc)}</dt><dd>{remaining ? duration(remaining) : '–'}</dd></div>
            </div>
          ) : (
            <div className="facts">
              <div><dt>Odometer</dt><dd>{dist(o.odometer_km, 0)}</dd></div>
              {s.vehicle.capacity_kwh && <div><dt>Usable battery</dt><dd>{kwh(s.vehicle.capacity_kwh, 0)}</dd></div>}
              {s.vehicle.model && <div><dt>Car</dt><dd>{[s.vehicle.model, s.vehicle.year].filter(Boolean).join(' · ')}</dd></div>}
            </div>
          )}
        </div>
        {s.vehicle.image && (
          <div className="hero-car">
            <img src={s.vehicle.image} alt={[s.vehicle.colour, s.vehicle.model].filter(Boolean).join(' ') || 'The car'} referrerPolicy="no-referrer" />
          </div>
        )}
      </div>

      <SinceCharge s={s} />

      <div className="grid cols-2">
        <div className="grid" style={{ alignContent: 'start' }}>
          <div className="grid tiles">
            <LastTrip s={s} />
            <LastCharge s={s} />
          </div>
          <Controls s={s} />
        </div>
        <Checks s={s} />
      </div>

      <SocHistory s={s} />

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
        <Activity />
      </div>
    </>
  )
}

/** Driving since the car was last charged; hidden while it charges. */
function SinceCharge({ s }: { s: Status }) {
  const c = s.last_charge
  const a = c?.after
  if (s.charge || !c || !a) return null
  const d = split(dist(a.distance_km))
  const e = split(efficiency(a.efficiency))
  const r = split(a.full_range_km != null ? dist(a.full_range_km, 0) : '–')
  return (
    <>
      <div className="section-label">Since the last charge, {day(c.ended_at ?? c.started_at)}</div>
      <div className="grid tiles">
        <Tile icon="route" label="Driven" value={d.value} unit={d.unit} sub={`${a.trips} ${a.trips === 1 ? 'trip' : 'trips'}`} />
        <Tile icon="leaf" label="Consumption" value={e.value} unit={e.unit} sub={a.energy_kwh != null ? `${kwh(a.energy_kwh)} used driving` : undefined} />
        <Tile icon="battery" label="Battery used" value={a.soc_used != null ? Math.round(a.soc_used) : '–'} unit={a.soc_used != null ? '%' : undefined} sub={`${pct(a.soc_from)} → ${pct(a.soc_to)}`} />
        <Tile icon="gauge" label="Full-charge range" value={r.value} unit={r.unit} sub={a.full_range_km != null ? 'At this rate, standby included' : 'Needs 5% of battery used'} />
      </div>
    </>
  )
}

function LastTrip({ s }: { s: Status }) {
  const t = s.last_trip
  if (!t) return <Tile icon="route" label="Last trip" value="–" sub="No trips yet" />
  const d = split(dist(t.distance_km))
  return (
    <Tile
      icon="route"
      label="Last trip"
      value={d.value}
      unit={d.unit}
      sub={<>{day(t.ended_at)}, {time(t.ended_at)} · {t.efficiency ? efficiency(t.efficiency) : duration(t.ended_at - t.started_at)}</>}
    />
  )
}

function LastCharge({ s }: { s: Status }) {
  const c = s.last_charge
  if (!c) return <Tile icon="bolt" label="Last charge" value="–" sub="No sessions yet" />
  const e = split(kwh(c.energy_billed_kwh))
  return (
    <Tile
      icon="bolt"
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

interface History {
  points: { t: number; soc: number; range_km: number | null; power_w: number | null; charging_status: string | null }[]
  spans: { kind: 'drive' | 'charge'; from: number; to: number }[]
}

/**
 * Battery, range and charging power as stacked panels on one time axis.
 * Separate panels rather than one chart with two y-scales: each keeps an honest axis.
 */
function SocHistory({ s }: { s: Status }) {
  const [days, setDays] = useState<number>(7)
  const from = useMemo(() => Math.floor((now() - days * 86400) / 600) * 600, [days])
  const { data } = useLive<History>(`/history?from=${from}`)
  const end = now()
  const target = s.obs?.target_soc ?? null

  const points = useMemo(() => {
    if (!data?.points.length) return []
    const rows = data.points.map((d) => ({
      t: Math.max(d.t, from),
      soc: d.soc,
      range: d.range_km != null ? distValue(d.range_km) : null,
      range_km: d.range_km,
      kw: d.charging_status === 'CHARGING' && d.power_w != null ? d.power_w / 1000 : 0,
    }))
    // Hold the last value to "now": the battery did not vanish, the car is just quiet.
    return [...rows, { ...rows[rows.length - 1], t: end }]
  }, [data, from]) // eslint-disable-line react-hooks/exhaustive-deps

  const spans = (data?.spans ?? []).map((x) => ({ ...x, from: Math.max(x.from, from), to: Math.min(x.to, end) })).filter((x) => x.to > x.from)
  const doing = (t: number) => spans.find((x) => t >= x.from && t <= x.to)?.kind
  const charged = points.some((p) => p.kw > 0)

  const ticks = useMemo(() => {
    const step = days === 1 ? 6 * 3600 : days === 7 ? 86400 : 5 * 86400
    const out = []
    for (let t = Math.ceil(from / step) * step; t < end; t += step) out.push(t)
    return out
  }, [from, days]) // eslint-disable-line react-hooks/exhaustive-deps

  const last = points.at(-1)
  const x = (show: boolean) => (
    <XAxis
      dataKey="t" type="number" domain={[from, end]} ticks={ticks} {...axis} hide={!show}
      tickFormatter={(t) => (days === 1 ? time(t) : day(t).split(',')[0])}
    />
  )
  const bands = spans.map((b) => (
    <ReferenceArea key={`${b.kind}${b.from}`} x1={b.from} x2={b.to} fill={b.kind === 'charge' ? 'var(--band-charge)' : 'var(--band-drive)'} fillOpacity={1} strokeOpacity={0} ifOverflow="hidden" />
  ))
  const grid = <CartesianGrid vertical={false} stroke="var(--grid)" />
  const cursor = <Tooltip cursor={{ stroke: 'var(--axis)' }} content={() => null} />
  const dot = { r: 4, stroke: 'var(--surface)', strokeWidth: 2 }
  const margin = { top: 6, right: 8, bottom: 0, left: 0 }
  const W = 48

  return (
    <div className="card">
      <div className="card-head">
        <h2>Battery and range</h2>
        <div className="seg" role="group" aria-label="Time span">
          {SPANS.map(([label, d]) => (
            <button key={d} className={d === days ? 'on' : ''} aria-pressed={d === days} onClick={() => setDays(d)}>{label}</button>
          ))}
        </div>
      </div>
      {points.length ? (
        <>
          <div className="legend">
            <span><i className="line" style={{ background: 'var(--series-1)' }} />Battery</span>
            <span><i className="line" style={{ background: 'var(--series-3)' }} />Range</span>
            {charged && <span><i className="line" style={{ background: 'var(--series-2)' }} />Charging power</span>}
            {target != null && <span><i className="dash" />Charge limit</span>}
            <span><i style={{ background: 'var(--band-charge)' }} />Charging</span>
            <span><i style={{ background: 'var(--band-drive)' }} />Driving</span>
          </div>

          <div className="panel">
            <div className="panel-title">Battery <span className="now">{pct(last?.soc)}</span></div>
            <div className="chart" style={{ height: 170 }}>
              <ResponsiveContainer>
                <AreaChart data={points} margin={margin} syncId="battery" syncMethod="value">
                  {bands}
                  {grid}
                  {x(false)}
                  <YAxis domain={[0, 100]} ticks={[0, 50, 100]} {...axis} axisLine={false} width={W} tickFormatter={(v) => `${v}%`} />
                  {target != null && <ReferenceLine y={target} stroke="var(--ink-2)" strokeDasharray="4 4" strokeWidth={1.5} ifOverflow="hidden" />}
                  <Tooltip
                    cursor={{ stroke: 'var(--axis)' }}
                    content={<Tip title={(d) => dateTime(d.t)} rows={(d) => [
                      { label: 'Battery', value: pct(d.soc), color: 'var(--series-1)' },
                      { label: 'Range', value: dist(d.range_km, 0), color: 'var(--series-3)' },
                      ...(doing(d.t) === 'charge' ? [{ label: 'Charging', value: d.kw ? kw(d.kw) : 'yes', color: 'var(--series-2)' }] : []),
                      ...(doing(d.t) === 'drive' ? [{ label: 'Driving', value: '' }] : []),
                    ]} />}
                  />
                  <Area
                    type="stepAfter" dataKey="soc" stroke="var(--series-1)" strokeWidth={2}
                    fill="var(--series-1)" fillOpacity={0.12} isAnimationActive={false} activeDot={dot}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="panel">
            <div className="panel-title">Range, {distUnit()} <span className="now">{dist(last?.range_km, 0)}</span></div>
            <div className="chart" style={{ height: charged ? 110 : 130 }}>
              <ResponsiveContainer>
                <LineChart data={points} margin={margin} syncId="battery" syncMethod="value">
                  {bands}
                  {grid}
                  {x(!charged)}
                  <YAxis domain={[0, 'auto']} tickCount={3} {...axis} axisLine={false} width={W} tickFormatter={(v) => `${Math.round(v)}`} />
                  {cursor}
                  <Line type="stepAfter" dataKey="range" stroke="var(--series-3)" strokeWidth={2} dot={false} isAnimationActive={false} activeDot={dot} connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {charged && (
            <div className="panel">
              <div className="panel-title">Charging power, kW <span className="now">{last?.kw ? kw(last.kw) : 'not charging'}</span></div>
              <div className="chart" style={{ height: 120 }}>
                <ResponsiveContainer>
                  <AreaChart data={points} margin={margin} syncId="battery" syncMethod="value">
                    {bands}
                    {grid}
                    {x(true)}
                    <YAxis domain={[0, 'auto']} tickCount={3} {...axis} axisLine={false} width={W} tickFormatter={(v) => `${Math.round(v)}`} />
                    {cursor}
                    <Area
                      type="stepAfter" dataKey="kw" stroke="var(--series-2)" strokeWidth={2}
                      fill="var(--series-2)" fillOpacity={0.12} isAnimationActive={false} activeDot={dot}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}
        </>
      ) : (
        <Empty>No readings in this period.</Empty>
      )}
      <p className="hint" style={{ marginTop: 10 }}>
        The car reports when it drives, charges or wakes, not on a schedule, so flat stretches are simply the last value it sent.
        See <Link to="/trips">Trips</Link> and <Link to="/charging">Charging</Link> for what happened in between.
      </p>
    </div>
  )
}
