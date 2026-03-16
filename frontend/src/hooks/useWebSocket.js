import { useEffect, useRef, useCallback, useState } from 'react';

const WS_URL  = process.env.REACT_APP_WS_URL  || 'ws://localhost:3000/ws';
const API_URL = process.env.REACT_APP_API_URL  || 'http://localhost:3000/api';

const RECONNECT_DELAY_MS = [500, 1000, 2000, 4000, 8000];

/**
 * useWebSocket
 * ------------
 * Manages a resilient WebSocket connection to the Game of Life backend.
 *
 * Returns:
 *   cells             – Float32Array of [x0,y0, x1,y1, ...] (flat, for canvas perf)
 *   meta              – { generation, totalAlive, perZone, lastUpdated }
 *   status            – 'connecting' | 'connected' | 'disconnected' | 'error'
 *   isRunning         – boolean: true when the simulation is computing generations
 *   reset             – async fn: POST /reset → flushes Redis + re-seeds all zones
 *   startSimulation   – async fn: POST /start → resumes worker computation
 *   stopSimulation    – async fn: POST /stop  → pauses worker computation
 *   setCells          – async fn: POST /set-cells → writes user cells to Redis
 *   latencyMs         – round-trip estimate in ms
 */
export function useWebSocket() {
  const wsRef            = useRef(null);
  const reconnectAttempt = useRef(0);
  const reconnectTimer   = useRef(null);
  const pingInterval     = useRef(null);
  const lastPingSent     = useRef(null);
  const mountedRef       = useRef(true);

  const [cells,     setCells_]   = useState(new Float32Array(0));
  const [meta,      setMeta]     = useState({
    generation: 0, totalAlive: 0, perZone: {}, lastUpdated: null,
  });
  const [status,    setStatus]   = useState('connecting');
  const [isRunning, setIsRunning] = useState(true);
  const [latencyMs, setLatencyMs] = useState(null);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setStatus('connecting');
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      reconnectAttempt.current = 0;
      setStatus('connected');

      // Ping every 20 s for latency measurement + keepalive
      pingInterval.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          lastPingSent.current = performance.now();
          ws.send('ping');
        }
      }, 20_000);
    };

    ws.onmessage = (event) => {
      if (!mountedRef.current) return;
      try {
        const msg = JSON.parse(event.data);

        if (msg.type === 'pong' && lastPingSent.current != null) {
          setLatencyMs(Math.round(performance.now() - lastPingSent.current));
          lastPingSent.current = null;
          return;
        }

        if (msg.type === 'heartbeat') return;

        // Simulation lifecycle messages
        if (msg.type === 'paused')  { setIsRunning(false); return; }
        if (msg.type === 'running') { setIsRunning(true);  return; }

        if (msg.type === 'state' && Array.isArray(msg.cells)) {
          const flat = new Float32Array(msg.cells.length * 2);
          for (let i = 0; i < msg.cells.length; i++) {
            flat[i * 2]     = msg.cells[i][0];
            flat[i * 2 + 1] = msg.cells[i][1];
          }
          setCells_(flat);
          setMeta({
            generation:  typeof msg.generation === 'number'
                           ? msg.generation
                           : parseInt(msg.generation, 10) || 0,
            totalAlive:  msg.total_alive ?? 0,
            perZone:     msg.per_zone    ?? {},
            lastUpdated: Date.now(),
          });
        }

        if (msg.type === 'error') {
          console.warn('[WS] Backend error:', msg.detail);
        }
      } catch (e) {
        console.error('[WS] Parse error:', e);
      }
    };

    ws.onerror = () => {
      if (!mountedRef.current) return;
      setStatus('error');
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      clearInterval(pingInterval.current);
      setStatus('disconnected');

      const delay = RECONNECT_DELAY_MS[
        Math.min(reconnectAttempt.current, RECONNECT_DELAY_MS.length - 1)
      ];
      reconnectAttempt.current++;
      reconnectTimer.current = setTimeout(connect, delay);
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      clearInterval(pingInterval.current);
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  /** Reset: POST /reset → backend flushes all Redis + re-seeds all zones. */
  const reset = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/reset`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return { ok: true, message: 'Reset OK' };
    } catch (err) {
      console.error('[reset]', err);
      return { ok: false, message: err.message };
    }
  }, []);

  /** Stop: POST /stop → workers pause, WS receives type:'paused' */
  const stopSimulation = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/stop`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setIsRunning(false);
      return { ok: true };
    } catch (err) {
      console.error('[stop]', err);
      return { ok: false, message: err.message };
    }
  }, []);

  /** Start: POST /start → workers resume, WS receives type:'running' */
  const startSimulation = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/start`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setIsRunning(true);
      return { ok: true };
    } catch (err) {
      console.error('[start]', err);
      return { ok: false, message: err.message };
    }
  }, []);

  /**
   * setCells: POST /set-cells → clears Redis + writes user-drawn cells.
   * @param {Set<string>} cellSet – Set of "x:y" strings
   */
  const setCells = useCallback(async (cellSet) => {
    const cells = Array.from(cellSet).map(s => {
      const [x, y] = s.split(':').map(Number);
      return [x, y];
    });
    try {
      const res = await fetch(`${API_URL}/set-cells`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ cells }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return { ok: true };
    } catch (err) {
      console.error('[set-cells]', err);
      return { ok: false, message: err.message };
    }
  }, []);

  return { cells, meta, status, isRunning, reset, startSimulation, stopSimulation, setCells, latencyMs };
}