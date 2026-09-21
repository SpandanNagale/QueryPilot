"use client";

import { ChartSpec } from "@/lib/types";

// Hand-rolled minimal SVG bar/line/pie renderer -- QueryPilot only ever
// needs one series against one categorical/temporal axis (app/agent/nodes/
// chart_detector.py picks exactly one of the three), so a full charting
// library is more than this needs.
const COLORS = ["#2dd4bf", "#f2b94e", "#f2685c", "#6b7688", "#8ea0b8", "#c98f3d"];

function toNumber(v: unknown): number {
  const n = typeof v === "number" ? v : parseFloat(String(v));
  return Number.isFinite(n) ? n : 0;
}

export function ChartView({ spec, rows }: { spec: ChartSpec; rows: Record<string, unknown>[] }) {
  const points = rows.slice(0, 20).map((r) => ({ x: String(r[spec.x] ?? ""), y: toNumber(r[spec.y]) }));
  if (points.length === 0) return null;

  return (
    <div className="rounded-lg border border-border-0 bg-surface-1 p-4">
      <div className="mb-3 text-xs font-telemetry text-ink-1">{spec.title}</div>
      {spec.type === "pie" ? <PieChart points={points} /> : <CartesianChart points={points} type={spec.type} />}
    </div>
  );
}

function CartesianChart({ points, type }: { points: { x: string; y: number }[]; type: "bar" | "line" }) {
  const width = 560;
  const height = 220;
  const padding = { top: 10, right: 10, bottom: 36, left: 44 };
  const innerW = width - padding.left - padding.right;
  const innerH = height - padding.top - padding.bottom;
  const maxY = Math.max(1, ...points.map((p) => p.y));
  const step = innerW / Math.max(1, points.length);

  const xFor = (i: number) => padding.left + i * step + step / 2;
  const yFor = (v: number) => padding.top + innerH - (v / maxY) * innerH;

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" role="img" aria-label="Chart">
      <line x1={padding.left} y1={padding.top} x2={padding.left} y2={padding.top + innerH} stroke="var(--border-1)" />
      <line
        x1={padding.left}
        y1={padding.top + innerH}
        x2={padding.left + innerW}
        y2={padding.top + innerH}
        stroke="var(--border-1)"
      />
      {[0, 0.5, 1].map((f) => (
        <text key={f} x={padding.left - 6} y={yFor(maxY * f) + 4} textAnchor="end" fontSize="9" fill="var(--ink-2)">
          {(maxY * f).toFixed(maxY < 10 ? 1 : 0)}
        </text>
      ))}
      {type === "bar" &&
        points.map((p, i) => {
          const barW = step * 0.6;
          const y = yFor(p.y);
          return (
            <rect
              key={i}
              x={xFor(i) - barW / 2}
              y={y}
              width={barW}
              height={padding.top + innerH - y}
              fill="var(--teal)"
              rx={2}
            />
          );
        })}
      {type === "line" && (
        <polyline
          fill="none"
          stroke="var(--teal)"
          strokeWidth={2}
          points={points.map((p, i) => `${xFor(i)},${yFor(p.y)}`).join(" ")}
        />
      )}
      {type === "line" &&
        points.map((p, i) => <circle key={i} cx={xFor(i)} cy={yFor(p.y)} r={2.5} fill="var(--teal)" />)}
      {points.map((p, i) => (
        <text
          key={`lbl-${i}`}
          x={xFor(i)}
          y={height - padding.bottom + 14}
          textAnchor="middle"
          fontSize="9"
          fill="var(--ink-2)"
          transform={points.length > 8 ? `rotate(30 ${xFor(i)} ${height - padding.bottom + 14})` : undefined}
        >
          {p.x.length > 10 ? `${p.x.slice(0, 9)}…` : p.x}
        </text>
      ))}
    </svg>
  );
}

function PieChart({ points }: { points: { x: string; y: number }[] }) {
  const total = points.reduce((s, p) => s + p.y, 0) || 1;
  const cx = 90;
  const cy = 90;
  const r = 70;

  // Cumulative start angle per slice, computed as a pure running-sum array
  // first (no mutation across iterations), then mapped into slice paths --
  // avoids reassigning a render-scope variable inside .map().
  const fractions = points.map((p) => (p.y / total) * Math.PI * 2);
  const startAngles = fractions.map((_, i) => (i === 0 ? -Math.PI / 2 : fractions.slice(0, i).reduce((s, f) => s + f, -Math.PI / 2)));

  const slices = points.map((p, i) => {
    const start = startAngles[i];
    const end = start + fractions[i];
    const large = fractions[i] > Math.PI ? 1 : 0;
    const x1 = cx + r * Math.cos(start);
    const y1 = cy + r * Math.sin(start);
    const x2 = cx + r * Math.cos(end);
    const y2 = cy + r * Math.sin(end);
    return { d: `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} Z`, color: COLORS[i % COLORS.length], p };
  });

  return (
    <div className="flex flex-wrap items-center gap-4">
      <svg viewBox="0 0 180 180" width={160} height={160} role="img" aria-label="Pie chart">
        {slices.map((s, i) => (
          <path key={i} d={s.d} fill={s.color} stroke="var(--surface-1)" strokeWidth={1} />
        ))}
      </svg>
      <ul className="flex flex-col gap-1 text-xs font-telemetry text-ink-1">
        {slices.map((s, i) => (
          <li key={i} className="flex items-center gap-2">
            <span className="inline-block size-2.5 rounded-full" style={{ background: s.color }} />
            <span className="text-ink-0">{s.p.x}</span>
            <span className="text-ink-2">{s.p.y}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
