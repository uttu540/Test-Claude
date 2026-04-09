import { useState, useEffect, useRef, useCallback } from 'react'

const WS_URL = 'ws://localhost:8000/ws'

const RECONNECT_DELAY_MS     = 3000
const MAX_RECONNECT_DELAY_MS = 30000
const BACKOFF_FACTOR         = 1.5
const HEARTBEAT_INTERVAL_MS  = 30000

/**
 * Low-level WebSocket client with auto-reconnect and exponential back-off.
 *
 *   const client = createWsClient({ onMessage, onStatus })
 *   client.connect()
 *   client.disconnect()   // call on cleanup
 *   client.send(data)     // JSON-serialises data before sending
 */
export function createWsClient({ onMessage, onStatus } = {}) {
  let socket         = null
  let reconnectTimer = null
  let heartbeatTimer = null
  let delay          = RECONNECT_DELAY_MS
  let destroyed      = false

  function notify(status) {
    onStatus?.(status)
  }

  function connect() {
    if (destroyed) return
    if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) return

    notify('connecting')

    try {
      socket = new WebSocket(WS_URL)
    } catch {
      scheduleReconnect()
      return
    }

    socket.onopen = () => {
      if (destroyed) { socket.close(); return }
      delay = RECONNECT_DELAY_MS
      notify('connected')
      // Send periodic keep-alive pings so the server's receive_text() loop doesn't block
      heartbeatTimer = setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) socket.send('ping')
      }, HEARTBEAT_INTERVAL_MS)
    }

    socket.onmessage = ({ data }) => {
      if (destroyed) return
      try {
        onMessage?.(JSON.parse(data))
      } catch {
        // ignore malformed JSON
      }
    }

    socket.onerror = () => {
      // onerror is always followed by onclose — no action needed here
    }

    socket.onclose = () => {
      clearInterval(heartbeatTimer)
      heartbeatTimer = null
      if (destroyed) return
      notify('disconnected')
      scheduleReconnect()
    }
  }

  function scheduleReconnect() {
    if (destroyed || reconnectTimer) return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      delay = Math.min(delay * BACKOFF_FACTOR, MAX_RECONNECT_DELAY_MS)
      connect()
    }, delay)
  }

  function disconnect() {
    destroyed = true
    clearTimeout(reconnectTimer)
    clearInterval(heartbeatTimer)
    reconnectTimer = null
    heartbeatTimer = null
    if (socket) {
      socket.onclose = null   // prevent re-schedule
      socket.close()
      socket = null
    }
    notify('disconnected')
  }

  function send(data) {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(typeof data === 'string' ? data : JSON.stringify(data))
    }
  }

  return { connect, disconnect, send }
}

/**
 * React hook — manages a WebSocket connection for the lifetime of the component.
 *
 * @param {function|null} onMessage  Called with each parsed message object.
 * @returns {{ wsStatus: string }}   wsStatus: 'connecting' | 'connected' | 'disconnected'
 */
export function useWebSocket(onMessage) {
  const [wsStatus, setWsStatus] = useState('disconnected')
  const onMessageRef = useRef(onMessage)

  useEffect(() => {
    onMessageRef.current = onMessage
  })

  useEffect(() => {
    const client = createWsClient({
      onMessage: (data) => onMessageRef.current?.(data),
      onStatus:  setWsStatus,
    })

    client.connect()
    return () => client.disconnect()
  }, []) // intentionally empty — one connection per mount

  return { wsStatus }
}
