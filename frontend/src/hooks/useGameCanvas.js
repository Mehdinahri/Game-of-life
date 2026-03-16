import { useEffect, useRef, useCallback } from 'react';

const GRID_W = 60;
const GRID_H = 50;

// Zone colour tints (slight variation for visual sharding feedback)
const ZONE_TINTS = [
  { r: 0, g: 255, b: 65  },   // zone-1 : classic phosphor green
  { r: 0, g: 230, b: 120 },   // zone-2 : slightly cyan-shifted
  { r: 0, g: 200, b: 80  },   // zone-3 : slightly darker green
];

// Drawn-cell colour (user-painted cells, shown while paused)
const DRAW_TINT = { r: 0, g: 220, b: 255 }; // bright cyan

function getZoneTint(x) {
  if (x < 20) return ZONE_TINTS[0];
  if (x < 40) return ZONE_TINTS[1];
  return ZONE_TINTS[2];
}

/**
 * useGameCanvas
 * -------------
 * Renders the Game of Life grid onto a <canvas> at 60 fps.
 *
 * In draw mode (isRunning === false) the canvas becomes interactive:
 *   - mousedown / touchstart → start drawing
 *   - mousemove  / touchmove  → paint cells along cursor path
 *   - mouseup    / touchend   → stop drawing
 *
 * Painted cells are stored as "x:y" strings in `drawnCells` (Set).
 * They are rendered in cyan to distinguish from simulation cells.
 *
 * @param canvasRef   React ref to the <canvas> element
 * @param cells       Float32Array of live cells from WebSocket
 * @param showZones   boolean – draw zone boundary lines
 * @param isRunning   boolean – when false, enable mouse drawing
 * @param drawnCells  Set<string> – "x:y" strings of user-drawn cells
 * @param onDrawCell  callback(x, y) – called when user draws a cell
 */
export function useGameCanvas(canvasRef, cells, showZones = true, isRunning = true, drawnCells = new Set(), onDrawCell = null) {
  const rafRef       = useRef(null);
  const cellsRef     = useRef(cells);
  const drawnRef     = useRef(drawnCells);
  const isRunningRef = useRef(isRunning);
  const onDrawRef    = useRef(onDrawCell);   // ← ref so event handlers never go stale
  const isPainting   = useRef(false);

  // Keep latest values accessible inside rAF/event handlers without stale closure
  useEffect(() => { cellsRef.current     = cells;      }, [cells]);
  useEffect(() => { drawnRef.current     = drawnCells; }, [drawnCells]);
  useEffect(() => { isRunningRef.current = isRunning;  }, [isRunning]);
  useEffect(() => { onDrawRef.current    = onDrawCell; }, [onDrawCell]);

  // ── Canvas → grid coordinate helpers ──────────────────────────────────────

  const canvasToGrid = useCallback((canvas, clientX, clientY) => {
    const rect  = canvas.getBoundingClientRect();
    const ratioX = GRID_W / rect.width;
    const ratioY = GRID_H / rect.height;
    const gx = Math.floor((clientX - rect.left) * ratioX);
    const gy = Math.floor((clientY - rect.top)  * ratioY);
    if (gx < 0 || gx >= GRID_W || gy < 0 || gy >= GRID_H) return null;
    return [gx, gy];
  }, []);

  // ── Render ─────────────────────────────────────────────────────────────────

  const render = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const W   = canvas.width;
    const H   = canvas.height;

    const cellW = W / GRID_W;
    const cellH = H / GRID_H;

    // Phosphor persistence: fade previous frame
    ctx.fillStyle = 'rgba(2, 12, 4, 0.72)';
    ctx.fillRect(0, 0, W, H);

    // Zone boundary lines
    if (showZones) {
      ctx.strokeStyle = 'rgba(0, 255, 65, 0.12)';
      ctx.lineWidth   = 2;
      [20, 40].forEach((zoneX) => {
        const px = zoneX * cellW;
        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, H);
        ctx.stroke();
      });
    }

    const pad    = Math.max(1, cellW * 0.06);
    const innerW = cellW - pad * 2;
    const innerH = cellH - pad * 2;

    // ── Draw simulation (live) cells ─────────────────────────────────────────
    const flat = cellsRef.current;
    const len  = flat.length;

    if (len > 0) {
      // Glow pass
      ctx.save();
      ctx.shadowBlur = Math.max(6, cellW * 1.2);
      for (let i = 0; i < len; i += 2) {
        const x    = flat[i];
        const y    = flat[i + 1];
        const tint = getZoneTint(x);
        ctx.shadowColor = `rgba(${tint.r},${tint.g},${tint.b},0.6)`;
        ctx.fillStyle   = `rgba(${tint.r},${tint.g},${tint.b},0.8)`;
        ctx.fillRect(x * cellW + pad, y * cellH + pad, innerW, innerH);
      }
      ctx.restore();

      // Crisp pass
      for (let i = 0; i < len; i += 2) {
        const x    = flat[i];
        const y    = flat[i + 1];
        const tint = getZoneTint(x);
        ctx.fillStyle = `rgb(${tint.r},${tint.g},${tint.b})`;
        ctx.fillRect(x * cellW + pad, y * cellH + pad, innerW, innerH);
      }
    }

    // ── Draw user-painted cells (draw mode) ───────────────────────────────────
    const drawn = drawnRef.current;
    if (drawn.size > 0) {
      const t = DRAW_TINT;
      ctx.save();
      ctx.shadowBlur  = Math.max(4, cellW * 2.2);
      ctx.shadowColor = `rgba(${t.r},${t.g},${t.b},0.7)`;
      ctx.fillStyle   = `rgba(${t.r},${t.g},${t.b},0.9)`;

      for (const key of drawn) {
        const [gx, gy] = key.split(':').map(Number);
        ctx.fillRect(gx * cellW + pad, gy * cellH + pad, innerW, innerH);
      }
      ctx.restore();

      // Crisp pass for drawn cells
      ctx.fillStyle = `rgb(${t.r},${t.g},${t.b})`;
      for (const key of drawn) {
        const [gx, gy] = key.split(':').map(Number);
        ctx.fillRect(gx * cellW + pad, gy * cellH + pad, innerW, innerH);
      }
    }
  }, [canvasRef, showZones]);

  // Trigger a single rAF whenever cells or drawn cells update
  useEffect(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(render);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [cells, drawnCells, render]);

  // ── Handle canvas resize ───────────────────────────────────────────────────
  const fitCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (canvas.width !== rect.width || canvas.height !== rect.height) {
      canvas.width  = rect.width;
      canvas.height = rect.height;
      render();
    }
  }, [canvasRef, render]);

  useEffect(() => {
    const ro = new ResizeObserver(fitCanvas);
    if (canvasRef.current) ro.observe(canvasRef.current);
    fitCanvas();
    return () => ro.disconnect();
  }, [canvasRef, fitCanvas]);

  // ── Mouse / Touch drawing ──────────────────────────────────────────────────

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const paintAt = (clientX, clientY) => {
      if (isRunningRef.current) return; // only in draw mode
      const coord = canvasToGrid(canvas, clientX, clientY);
      if (!coord) return;
      const [gx, gy] = coord;
      onDrawRef.current?.(gx, gy);   // ← always calls latest callback
    };

    const onMouseDown = (e) => {
      if (isRunningRef.current) return;
      isPainting.current = true;
      paintAt(e.clientX, e.clientY);
    };

    const onMouseMove = (e) => {
      if (!isPainting.current) return;
      paintAt(e.clientX, e.clientY);
    };

    const onMouseUp = () => { isPainting.current = false; };

    const onTouchStart = (e) => {
      if (isRunningRef.current) return;
      isPainting.current = true;
      const t = e.touches[0];
      paintAt(t.clientX, t.clientY);
      e.preventDefault();
    };

    const onTouchMove = (e) => {
      if (!isPainting.current) return;
      const t = e.touches[0];
      paintAt(t.clientX, t.clientY);
      e.preventDefault();
    };

    const onTouchEnd = () => { isPainting.current = false; };

    canvas.addEventListener('mousedown',  onMouseDown);
    canvas.addEventListener('mousemove',  onMouseMove);
    window.addEventListener('mouseup',    onMouseUp);
    canvas.addEventListener('touchstart', onTouchStart, { passive: false });
    canvas.addEventListener('touchmove',  onTouchMove,  { passive: false });
    canvas.addEventListener('touchend',   onTouchEnd);

    return () => {
      canvas.removeEventListener('mousedown',  onMouseDown);
      canvas.removeEventListener('mousemove',  onMouseMove);
      window.removeEventListener('mouseup',    onMouseUp);
      canvas.removeEventListener('touchstart', onTouchStart);
      canvas.removeEventListener('touchmove',  onTouchMove);
      canvas.removeEventListener('touchend',   onTouchEnd);
    };
  }, [canvasRef, canvasToGrid]); // intentionally stable — uses refs for dynamic values
}
