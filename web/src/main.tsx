import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'

const container = document.getElementById('root')
if (!container) throw new Error('#root not found')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// Register the service worker for offline shell and installability.
// Registered after load so it never competes with the first paint.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Not fatal: the app works without it, just without an offline shell.
    })
  })
}
