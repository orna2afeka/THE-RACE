import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api and /ws to uvicorn on 8000, so the app talks to
// its own origin with relative URLs. That is not a convenience: in production
// FastAPI static-serves this build and the origin genuinely IS the backend, so
// proxying in dev means there is no base-URL switch to get wrong on race day.
export default defineConfig({
  plugins: [react()],
  // MapLibre starts a MODULE worker for any URL not ending in .cjs, so the
  // worker Vite bundles for it must be ES format, not the default iife.
  worker: { format: 'es' },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
