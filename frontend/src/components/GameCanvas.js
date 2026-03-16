import React, { useRef } from 'react';
import { useGameCanvas } from '../hooks/useGameCanvas';
import './GameCanvas.css';

export default function GameCanvas({ cells, showZones, isRunning, drawnCells, onDrawCell }) {
  const canvasRef = useRef(null);
  useGameCanvas(canvasRef, cells, showZones, isRunning, drawnCells, onDrawCell);

  return (
    <div className={`canvas-wrapper ${!isRunning ? 'canvas-wrapper--draw' : ''}`}>
      <canvas
        ref={canvasRef}
        className="game-canvas"
        aria-label="Game of Life grid"
        style={{ cursor: isRunning ? 'default' : 'crosshair' }}
      />
      {/* Draw-mode overlay label */}
      {!isRunning && (
        <div className="canvas-draw-hint">
          ✏ CLICK &amp; DRAG TO DRAW CELLS
        </div>
      )}
      {/* Corner decorations */}
      <span className="corner corner--tl" />
      <span className="corner corner--tr" />
      <span className="corner corner--bl" />
      <span className="corner corner--br" />
    </div>
  );
}
