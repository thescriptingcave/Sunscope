import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API is on :8000 and this dev server on :5173, so in development the two
// are different origins and CORS applies (configured API-side). In production
// the built bundle is served *by* the API at `/`, making it same-origin and
// removing CORS from the picture entirely.
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Proxying /api in dev avoids CORS entirely for the common case, while
      // leaving the API's own CORS config in place for a genuinely split
      // deployment.
      '/api': { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
