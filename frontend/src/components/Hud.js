import React, { useState, useEffect, useRef } from 'react';
import './Hud.css';

const ZONE_LABELS = ['zone-1', 'zone-2', 'zone-3'];
const ZONE_NAMES  = ['SECTOR A', 'SECTOR B', 'SECTOR C'];

function BarMeter({ value, max, color = 'var(--phosphor)' }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="bar-track">
      <div className="bar-fill" style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}

function BlinkDot({ active }) {
  return <span className={`blink-dot ${active ? 'blink-dot--on' : ''}`} />;
}

function Counter({ value }) {
  const prevRef = useRef(value);
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (value !== prevRef.current) {
      prevRef.current = value;
      setFlash(true);
      const t = setTimeout(() => setFlash(false), 180);
      return () => clearTimeout(t);
    }
  }, [value]);

  return (
    <span className={`counter ${flash ? 'counter--flash' : ''}`}>
      {value.toLocaleString()}
    </span>
  );
}

export default function Hud({
  meta, status, latencyMs,
  isRunning, isBusy, drawnCount,
  onStart, onStop, onClear, onReset,
}) {
  const { generation, totalAlive, perZone } = meta;
  const maxZone  = Math.max(...ZONE_LABELS.map(z => perZone[z] ?? 0), 1);
  const tickRate = useTickRate(generation);

  return (
    <aside className="hud">

      {/* ── Header ── */}
      <header className="hud__header">
        <span className="hud__title">GAME OF LIFE</span>
        <span className="hud__subtitle">DISTRIBUTED ENGINE v1.0</span>
      </header>

      <div className="hud__divider" />

      {/* ── Connection status ── */}
      <section className="hud__section">
        <div className="hud__row">
          <span className="hud__label">SYS STATUS</span>
          <span className={`hud__status hud__status--${status}`}>
            <BlinkDot active={status === 'connected'} />
            {status.toUpperCase()}
          </span>
        </div>
        {latencyMs != null && (
          <div className="hud__row">
            <span className="hud__label">LATENCY</span>
            <span className="hud__value">{latencyMs} ms</span>
          </div>
        )}
      </section>

      <div className="hud__divider" />

      {/* ── Simulation Mode Banner ── */}
      <section className="hud__section">
        {isRunning ? (
          <div className="hud__mode-badge hud__mode-badge--running">
            <span className="hud__mode-dot" /> RUNNING
          </div>
        ) : (
          <div className="hud__mode-badge hud__mode-badge--paused">
            ✏ DRAW MODE
          </div>
        )}
        {!isRunning && drawnCount > 0 && (
          <div className="hud__row">
            <span className="hud__label">PENDING CELLS</span>
            <span className="hud__value hud__value--cyan">{drawnCount.toLocaleString()}</span>
          </div>
        )}
        {!isRunning && drawnCount === 0 && (
          <p className="hud__draw-hint">Click the grid to place cells, then press START.</p>
        )}
      </section>

      <div className="hud__divider" />

      {/* ── Simulation stats ── */}
      <section className="hud__section">
        <div className="hud__row">
          <span className="hud__label">GENERATION</span>
          <Counter value={generation} />
        </div>
        <div className="hud__row">
          <span className="hud__label">ALIVE CELLS</span>
          <Counter value={totalAlive} />
        </div>
        <div className="hud__row">
          <span className="hud__label">TICK RATE</span>
          <span className="hud__value">
            {tickRate != null ? `${tickRate.toFixed(1)} gen/s` : '—'}
          </span>
        </div>
      </section>

      <div className="hud__divider" />

      {/* ── Per-zone breakdown ── */}
      <section className="hud__section">
        <div className="hud__section-title">SHARD DISTRIBUTION</div>
        {ZONE_LABELS.map((zone, i) => {
          const count  = perZone[zone] ?? 0;
          const colors = ['var(--phosphor)', 'rgb(0,230,120)', 'rgb(0,200,80)'];
          return (
            <div key={zone} className="hud__zone">
              <div className="hud__row hud__row--compact">
                <span className="hud__label hud__label--zone">{ZONE_NAMES[i]}</span>
                <span className="hud__value hud__value--small">{count.toLocaleString()}</span>
              </div>
              <BarMeter value={count} max={maxZone} color={colors[i]} />
            </div>
          );
        })}
      </section>

      <div className="hud__divider" />

      {/* ── Legend ── */}
      <section className="hud__section">
        <div className="hud__section-title">ZONE MAP</div>
        <div className="hud__legend">
          {[
            { label: 'A  x:0–19',   color: 'var(--phosphor)' },
            { label: 'B  x:20–39',  color: 'rgb(0,230,120)' },
            { label: 'C  x:40–59',  color: 'rgb(0,200,80)' },
          ].map(({ label, color }) => (
            <div key={label} className="hud__legend-item">
              <span className="hud__legend-dot" style={{ background: color }} />
              <span className="hud__label">{label}</span>
            </div>
          ))}
        </div>
      </section>

      <div className="hud__spacer" />

      {/* ── Control Buttons ── */}
      <section className="hud__controls">

        {/* START */}
        <button
          id="btn-start"
          className="hud__btn hud__btn--start"
          onClick={onStart}
          disabled={isBusy || isRunning}
          aria-label="Start simulation"
        >
          ▶ START
        </button>

        {/* STOP */}
        <button
          id="btn-stop"
          className="hud__btn hud__btn--stop"
          onClick={onStop}
          disabled={isBusy || !isRunning}
          aria-label="Stop simulation"
        >
          ■ STOP
        </button>

        {/* CLEAR drawn cells */}
        <button
          id="btn-clear"
          className="hud__btn hud__btn--clear"
          onClick={onClear}
          disabled={isBusy || isRunning || drawnCount === 0}
          aria-label="Clear drawn cells"
        >
          ✕ CLEAR
        </button>

        {/* RESET (random reseed) */}
        <button
          id="btn-reset"
          className="hud__btn hud__btn--reset"
          onClick={onReset}
          disabled={isBusy}
          aria-label="Reset and reseed"
        >
          ⟳ RESET
        </button>

      </section>

      <p className="hud__footer">
        60×50 GRID · 3 REDIS SHARDS
      </p>
    </aside>
  );
}

/** Measures actual tick rate from generation counter changes */
function useTickRate(generation) {
  const samples    = useRef([]);
  const [rate, setRate] = useState(null);

  useEffect(() => {
    const now = Date.now();
    samples.current.push({ gen: generation, t: now });
    const cutoff = now - 5000;
    samples.current = samples.current.filter(s => s.t >= cutoff).slice(-10);

    if (samples.current.length >= 2) {
      const first = samples.current[0];
      const last  = samples.current[samples.current.length - 1];
      const dt    = (last.t - first.t) / 1000;
      const dg    = last.gen - first.gen;
      if (dt > 0 && dg > 0) setRate(dg / dt);
    }
  }, [generation]);

  return rate;
}
