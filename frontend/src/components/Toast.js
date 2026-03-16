import React, { useEffect } from 'react';
import './Toast.css';

export default function Toast({ message, type = 'info', onClose }) {
  useEffect(() => {
    const t = setTimeout(onClose, 3000);
    return () => clearTimeout(t);
  }, [onClose]);

  return (
    <div className={`toast toast--${type}`} onClick={onClose}>
      <span className="toast__icon">
        {type === 'success' ? '✓' : type === 'error' ? '✗' : 'ℹ'}
      </span>
      {message}
    </div>
  );
}
