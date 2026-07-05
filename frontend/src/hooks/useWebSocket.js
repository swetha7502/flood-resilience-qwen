import { useEffect, useRef, useCallback } from 'react';

/**
 * useWebSocket
 *
 * Connects to the FloodGuard AI backend WebSocket endpoint.
 * Parses every incoming JSON message and passes it to `onMessage`.
 * Auto-reconnects after RECONNECT_DELAY ms if the connection closes.
 *
 * Backend URL is read from:
 *   VITE_BACKEND_URL environment variable (set in .env)
 *   Fallback: http://localhost:8000
 *
 * @param {function} onMessage  - Called with the parsed message object on every WS message.
 * @param {boolean}  enabled    - Set to false to skip connecting.
 */

const RECONNECT_DELAY = 2000; // ms

function getWsUrl() {
  // import.meta.env is Vite's way of reading env vars at build time.
  // VITE_BACKEND_URL is set in .env (e.g., http://localhost:8000 or https://prod-host.com)
  const base = import.meta.env.VITE_BACKEND_URL || 'http://localhost:8000';
  // Convert http(s) → ws(s) so the env var stays as a plain HTTP URL.
  return base.replace(/^http/, 'ws') + '/ws';
}

export function useWebSocket(onMessage, enabled = true) {
  const wsRef    = useRef(null);
  const timerRef = useRef(null);
  const onMsgRef = useRef(onMessage);

  // Keep the callback ref current without triggering a reconnect.
  useEffect(() => {
    onMsgRef.current = onMessage;
  }, [onMessage]);

  const connect = useCallback(() => {
    if (!enabled) return;

    const url = getWsUrl();
    console.log('[FloodGuard WS] Connecting to', url);
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log('[FloodGuard WS] Connected ✓');
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        console.log('[FloodGuard WS] ←', msg.type, msg);
        if (onMsgRef.current) {
          onMsgRef.current(msg);
        }
      } catch (err) {
        console.warn('[FloodGuard WS] Failed to parse message:', event.data, err);
      }
    };

    ws.onerror = (err) => {
      console.warn('[FloodGuard WS] Error:', err);
    };

    ws.onclose = (evt) => {
      console.log(
        `[FloodGuard WS] Closed (code=${evt.code}). Reconnecting in ${RECONNECT_DELAY}ms…`
      );
      timerRef.current = setTimeout(connect, RECONNECT_DELAY);
    };
  }, [enabled]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    connect();

    return () => {
      // Cleanup: prevent reconnect timer and close socket on unmount.
      clearTimeout(timerRef.current);
      if (wsRef.current) {
        // Override onclose so cleanup doesn't trigger another reconnect.
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [connect]);
}
