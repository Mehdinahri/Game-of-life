import React, { useState, useCallback, useRef } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import GameCanvas from './components/GameCanvas';
import Hud from './components/Hud';
import Toast from './components/Toast';
import './App.css';

export default function App() {
  const {
    cells,
    meta,
    status,
    isRunning,
    reset,
    startSimulation,
    stopSimulation,
    setCells,
    latencyMs,
  } = useWebSocket();

  // ── Local state ────────────────────────────────────────────────────────────
  const [drawnCells,  setDrawnCells]  = useState(() => new Set());
  const [showZones,   setShowZones]   = useState(true);
  const [toast,       setToast]       = useState(null);
  const [isBusy,      setIsBusy]      = useState(false);   // any async op in flight

  const dismissToast = useCallback(() => setToast(null), []);

  // Keep a ref to drawnCells so callbacks don't get stale
  const drawnRef = useRef(drawnCells);
  drawnRef.current = drawnCells;

  // ── Draw cell handler (called by useGameCanvas on each painted grid cell) ──
  const handleDrawCell = useCallback((x, y) => {
    const key = `${x}:${y}`;
    setDrawnCells(prev => {
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  }, []);

  // ── STOP: pause workers, enter draw mode ───────────────────────────────────
  const handleStop = useCallback(async () => {
    if (isBusy) return;
    setIsBusy(true);
    try {
      const res = await stopSimulation();
      if (!res.ok) {
        setToast({ message: `Stop failed: ${res.message}`, type: 'error' });
      }
    } finally {
      setIsBusy(false);
    }
  }, [isBusy, stopSimulation]);

  // ── START: send drawn cells (if any) then resume workers ──────────────────
  const handleStart = useCallback(async () => {
    if (isBusy) return;
    setIsBusy(true);
    try {
      if (drawnRef.current.size > 0) {
        // Send user-drawn cells to Redis first
        const res = await setCells(drawnRef.current);
        if (!res.ok) {
          setToast({ message: `Failed to set cells: ${res.message}`, type: 'error' });
          return;
        }
        setDrawnCells(new Set()); // clear after sending
      }
      const res = await startSimulation();
      if (!res.ok) {
        setToast({ message: `Start failed: ${res.message}`, type: 'error' });
      }
    } finally {
      setIsBusy(false);
    }
  }, [isBusy, setCells, startSimulation]);

  // ── CLEAR: erase user-drawn cells ─────────────────────────────────────────
  const handleClear = useCallback(() => {
    setDrawnCells(new Set());
  }, []);

  // ── RESET: random reseed ───────────────────────────────────────────────────
  const handleReset = useCallback(async () => {
    if (isBusy) return;
    setIsBusy(true);
    try {
      const result = await reset();
      setDrawnCells(new Set());
      setToast({
        message: result.ok ? 'Grid reseeded!' : `Reset failed: ${result.message}`,
        type:    result.ok ? 'success' : 'error',
      });
    } finally {
      setIsBusy(false);
    }
  }, [isBusy, reset]);

  return (
    <div className="app">
      <Hud
        meta={meta}
        status={status}
        latencyMs={latencyMs}
        isRunning={isRunning}
        isBusy={isBusy}
        drawnCount={drawnCells.size}
        onStart={handleStart}
        onStop={handleStop}
        onClear={handleClear}
        onReset={handleReset}
      />

      <main className="app__main">
        {/* Toolbar */}
        <div className="toolbar">
          <span className="toolbar__brand">
            <span className="toolbar__brand-accent">//</span> CONWAY.DISTRIBUTED
          </span>

          <label className="toolbar__toggle">
            <input
              type="checkbox"
              checked={showZones}
              onChange={e => setShowZones(e.target.checked)}
            />
            <span>SHOW ZONES</span>
          </label>

          <span className={`toolbar__ws-badge toolbar__ws-badge--${status}`}>
            {status === 'connected'    && '● LIVE'}
            {status === 'connecting'   && '◌ CONNECTING'}
            {status === 'disconnected' && '○ OFFLINE'}
            {status === 'error'        && '✕ ERROR'}
          </span>
        </div>

        {/* Canvas */}
        <GameCanvas
          cells={cells}
          showZones={showZones}
          isRunning={isRunning}
          drawnCells={drawnCells}
          onDrawCell={handleDrawCell}
        />
      </main>

      {toast && (
        <Toast
          message={toast.message}
          type={toast.type}
          onClose={dismissToast}
        />
      )}
    </div>
  );
}
