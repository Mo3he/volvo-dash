import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { useLive } from '../api'
import { axis, Empty, Tile, Tip, split, usePeriod } from '../components'
import {
  bucketLabel, dist, distUnit, distValue, duration, efficiency, efficiencyUnit, efficiencyValue, kwh, money,
} from '../format'

const UNIT_WORD = { day: 'day', week: 'week', month: 'month' } as const

export function Stats() {
  const { query, picker } = usePeriod()
  const { data } = useLive<any>(`/stats${query}`)
  if (!data) return null

  const t = data.totals
  const unit = data.bucket as 'day' | 'week' | 'month'
  const series = data.series.map((s: any) => ({
    ...s,
    label: bucketLabel(s.bucket, unit),
    distance: distValue(s.distance_km),
    charged: s.ac_kwh + s.dc_kwh,
  }))
  const bands = data.by_temperature.map((b: any) => ({
    ...b, label: `${b.from_c} to ${b.to_c} °C`, value: efficiencyValue(b.efficiency),
  }))
  const d = split(dist(t.distance_km, 0))
  const eff = split(efficiency(t.efficiency))
  const charged = split(kwh(t.charged_kwh, 0))
  const title = (row: any) => (unit === 'week' ? `Week of ${row.label}` : row.label)

  return (
    <>
      <div className="page-head">
        <h1>Stats</h1>
        {picker}
      </div>

      <div className="grid tiles">
        <Tile label="Distance" value={d.value} unit={d.unit} sub={`${t.trips} trips, ${duration(t.drive_seconds)}`} />
        <Tile label="Average consumption" value={eff.value} unit={eff.unit} />
        <Tile label="Energy charged" value={charged.value} unit={charged.unit} sub={`${t.charges} sessions`} />
        <Tile label="Charging cost" value={money(t.cost)} sub={t.cost_per_100km ? `${money(t.cost_per_100km)} per 100 km` : undefined} />
      </div>

      {!series.length ? (
        <div className="card"><Empty>Nothing logged in this period yet.</Empty></div>
      ) : (
        <>
          <div className="card">
            <h2>Distance per {UNIT_WORD[unit]}</h2>
            <Bars data={series}>
              <YAxis {...axis} axisLine={false} width={44} />
              <Tooltip
                cursor={{ fill: 'var(--wash)' }}
                content={<Tip title={title} rows={(r) => [{ label: 'Distance', value: dist(r.distance_km), color: 'var(--series-1)' }]} />}
              />
              <Bar dataKey="distance" fill="var(--series-1)" radius={[4, 4, 0, 0]} maxBarSize={24} isAnimationActive={false} />
            </Bars>
          </div>

          <div className="card">
            <h2>Energy charged per {UNIT_WORD[unit]}</h2>
            <div className="legend">
              <span><i style={{ background: 'var(--series-1)' }} />AC</span>
              <span><i style={{ background: 'var(--series-2)' }} />DC fast charging</span>
            </div>
            <Bars data={series}>
              <YAxis {...axis} axisLine={false} width={44} />
              <Tooltip
                cursor={{ fill: 'var(--wash)' }}
                content={<Tip title={title} rows={(r) => [
                  { label: 'AC', value: kwh(r.ac_kwh), color: 'var(--series-1)' },
                  { label: 'DC', value: kwh(r.dc_kwh), color: 'var(--series-2)' },
                  { label: 'Cost', value: money(r.cost) },
                ]} />}
              />
              <Bar dataKey="ac_kwh" stackId="e" fill="var(--series-1)" maxBarSize={24} isAnimationActive={false} stroke="var(--surface)" strokeWidth={2}>
                {/* Round the top only where no DC segment sits on it. */}
                {series.map((r: any) => <Cell key={r.bucket} radius={(r.dc_kwh ? 0 : [4, 4, 0, 0]) as any} />)}
              </Bar>
              <Bar dataKey="dc_kwh" stackId="e" fill="var(--series-2)" radius={[4, 4, 0, 0]} maxBarSize={24} isAnimationActive={false} stroke="var(--surface)" strokeWidth={2} />
            </Bars>
          </div>

          <div className="card">
            <h2>Consumption by outside temperature</h2>
            {bands.length ? (
              <>
                <Bars data={bands}>
                  <YAxis {...axis} axisLine={false} width={44} />
                  <Tooltip
                    cursor={{ fill: 'var(--wash)' }}
                    content={<Tip title={(r) => r.label} rows={(r) => [
                      { label: 'Consumption', value: efficiency(r.efficiency), color: 'var(--series-1)' },
                      { label: 'Based on', value: `${r.trips} trips, ${dist(r.distance_km, 0)}` },
                    ]} />}
                  />
                  <Bar dataKey="value" fill="var(--series-1)" radius={[4, 4, 0, 0]} maxBarSize={24} isAnimationActive={false} />
                </Bars>
                <p className="hint" style={{ marginTop: 8 }}>
                  In {efficiencyUnit()}. Each bar is total energy over total distance for trips in that temperature range,
                  and ranges with under 20 km of driving are left out.
                </p>
              </>
            ) : (
              <Empty>Needs at least 20 {distUnit() === 'mi' ? 'km (12 mi)' : 'km'} of trips with a known temperature.</Empty>
            )}
          </div>
        </>
      )}
    </>
  )
}

function Bars({ data, children }: { data: any[]; children: React.ReactNode }) {
  return (
    <div className="chart" style={{ height: 220 }}>
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="label" {...axis} minTickGap={24} />
          {children}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
