import { useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, useLive, type AfterCharge, type Charge } from '../api'
import { axis, Empty, Tile, Tip, split, useAction, usePeriod } from '../components'
import { currencySymbol, dateTime, day, dist, duration, efficiency, kw, kwh, money, pct, time } from '../format'

export function Charging() {
  const { query, picker } = usePeriod()
  const { data: charges, reload } = useLive<Charge[]>(`/charges${query}`)
  const { data: stats, reload: reloadStats } = useLive<any>(`/stats${query}`)
  const [open, setOpen] = useState<number | null>(null)

  const t = stats?.totals
  const e = split(kwh(t?.charged_kwh, 0))
  const fast = t?.charged_kwh ? Math.round((t.dc_kwh / t.charged_kwh) * 100) : 0

  return (
    <>
      <div className="page-head">
        <h1>Charging</h1>
        <div className="row">
          {picker}
          <a className="btn" href={`/api/export/charges.csv${query}`}>Export CSV</a>
        </div>
      </div>

      <div className="grid tiles">
        <Tile label="Energy charged" value={e.value} unit={e.unit} sub={`${t?.charges ?? 0} sessions`} />
        <Tile
          label="Cost" value={money(t?.cost)}
          sub={t?.unpriced_charges ? `${t.unpriced_charges} sessions have no price yet` : t?.cost_per_100km ? `${money(t.cost_per_100km)} per 100 km driven` : undefined}
        />
        <Tile label="Fast charging" value={`${fast}%`} sub={`${kwh(t?.dc_kwh, 0)} on DC`} />
      </div>

      <div className="card">
        {!charges ? null : !charges.length ? (
          <Empty>No charging sessions in this period.</Empty>
        ) : (
          <div className="list">
            {charges.map((c) => (
              <div className="item" key={c.id} style={c === charges[0] ? { borderTop: 0 } : undefined}>
                <button className="item-row cols-4" aria-expanded={open === c.id} onClick={() => setOpen(open === c.id ? null : c.id)}>
                  <span className="when nums soft">{day(c.started_at)}</span>
                  <span className="title">
                    {c.place_name || 'Unknown place'}{' '}
                    {c.charge_type && <span className="pill">{c.charge_type}</span>}
                  </span>
                  <span className="right nums"><strong>{kwh(c.energy_billed_kwh)}</strong></span>
                  <span className="right nums soft hide-s">{pct(c.soc_start)} → {pct(c.soc_end)}</span>
                  <span className="right nums soft hide-s" title="Driven after this charge">{c.after ? `${dist(c.after.distance_km, 0)}${c.after.ongoing ? ' so far' : ''}` : ''}</span>
                  <span className="right nums soft hide-s">{money(c.cost)}</span>
                </button>
                {open === c.id && <ChargeDetail id={c.id} onSaved={() => { reload(); reloadStats() }} />}
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  )
}

function ChargeDetail({ id, onSaved }: { id: number; onSaved: () => void }) {
  const { data: c, reload } = useLive<Charge>(`/charges/${id}`)
  if (!c) return null
  const samples = (c.samples ?? []).filter((s) => s.power_w != null).map((s) => ({ ...s, kw: s.power_w! / 1000 }))
  const seconds = (c.ended_at ?? c.started_at) - c.started_at

  return (
    <div className="item-body">
      <div className="facts">
        <div><dt>When</dt><dd>{time(c.started_at)} – {time(c.ended_at)} ({duration(seconds)})</dd></div>
        <div><dt>Battery</dt><dd>{pct(c.soc_start)} → {pct(c.soc_end)}</dd></div>
        <div><dt>Average power</dt><dd>{kw(c.avg_power_kw)}</dd></div>
        <div><dt>Peak power</dt><dd>{kw(c.max_power_kw)}</dd></div>
        <div><dt>From charging power</dt><dd>{kwh(c.energy_metered_kwh)}</dd></div>
        <div><dt>From battery level</dt><dd>{kwh(c.energy_soc_kwh)}</dd></div>
      </div>
      <p className="hint">
        {c.energy_override != null
          ? 'Energy was entered by hand.'
          : c.energy_source === 'metered'
            ? 'Energy is the charging power the car reported, added up over the session.'
            : 'Energy is estimated from the rise in battery level, because the car reported power too rarely to add up.'}
      </p>

      {samples.length > 2 && (
        <div className="chart" style={{ height: 180 }}>
          <ResponsiveContainer>
            <LineChart data={samples} margin={{ top: 6, right: 28, bottom: 0, left: -18 }}>
              <CartesianGrid vertical={false} stroke="var(--grid)" />
              <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} {...axis} tickFormatter={time} tickCount={6} />
              <YAxis {...axis} axisLine={false} domain={[0, 'auto']} tickFormatter={(v) => `${v}`} unit=" kW" width={70} />
              <Tooltip
                cursor={{ stroke: 'var(--axis)' }}
                content={<Tip title={(d) => dateTime(d.ts)} rows={(d) => [
                  { label: 'Power', value: kw(d.kw), color: 'var(--series-1)' },
                  { label: 'Battery', value: pct(d.soc) },
                ]} />}
              />
              <Line
                type="monotone" dataKey="kw" stroke="var(--series-1)" strokeWidth={2} dot={false} isAnimationActive={false}
                activeDot={{ r: 4, stroke: 'var(--surface)', strokeWidth: 2 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {c.after && <AfterThisCharge a={c.after} />}

      <ChargeEdit c={c} onSaved={() => { reload(); onSaved() }} />
    </div>
  )
}

function AfterThisCharge({ a }: { a: AfterCharge }) {
  return (
    <div className="stack">
      <h3>{a.ongoing ? 'Since this charge' : 'After this charge'}</h3>
      <div className="facts">
        <div><dt>Driven</dt><dd>{dist(a.distance_km)} in {a.trips} {a.trips === 1 ? 'trip' : 'trips'}</dd></div>
        <div><dt>Energy used</dt><dd>{kwh(a.energy_kwh)}</dd></div>
        <div><dt>Consumption</dt><dd>{efficiency(a.efficiency)}</dd></div>
        <div><dt>Battery</dt><dd>{pct(a.soc_from)} → {pct(a.soc_to)}{a.ongoing ? ' now' : ''}</dd></div>
        <div><dt>Range on a full charge</dt><dd>{a.full_range_km != null ? `about ${dist(a.full_range_km, 0)}` : 'Not enough battery used yet'}</dd></div>
      </div>
      <p className="hint">
        {a.ongoing ? 'Counts up until the next charge. ' : `Until the next charge on ${dateTime(a.until)}. `}
        Battery used includes standby and climate while parked, so the full-charge range is what you would get in everyday use, not just while driving.
      </p>
    </div>
  )
}

function ChargeEdit({ c, onSaved }: { c: Charge; onSaved: () => void }) {
  const [form, setForm] = useState({
    place_name: c.place_name ?? '',
    price_per_kwh: c.price_per_kwh?.toString() ?? '',
    cost_override: c.cost_override?.toString() ?? '',
    energy_override: c.energy_override?.toString() ?? '',
    note: c.note ?? '',
  })
  const { busy, run, note } = useAction()
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })
  const cur = currencySymbol()

  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault()
        run(async () => {
          await api(`/charges/${c.id}`, 'PATCH', form)
          onSaved()
          return 'Saved.'
        })
      }}
    >
      <label className="field">Place<input type="text" value={form.place_name} onChange={set('place_name')} /></label>
      <label className="field">Price per kWh ({cur})<input type="number" step="any" min="0" value={form.price_per_kwh} onChange={set('price_per_kwh')} /></label>
      <label className="field">
        Total cost, if you know it ({cur})
        <input type="number" step="any" min="0" value={form.cost_override} placeholder={c.cost != null && c.cost_override == null ? String(c.cost) : ''} onChange={set('cost_override')} />
      </label>
      <label className="field">
        Energy from the receipt (kWh)
        <input type="number" step="any" min="0" value={form.energy_override} placeholder={c.energy_kwh?.toString() ?? ''} onChange={set('energy_override')} />
      </label>
      <label className="field" style={{ gridColumn: '1 / -1' }}>Note<input type="text" value={form.note} onChange={set('note')} /></label>
      <div className="row">
        <button className="btn" disabled={busy}>Save</button>
        {note}
      </div>
    </form>
  )
}
