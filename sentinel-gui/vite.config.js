import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Ports here match sentinel-ai's default SENTINEL_GUI_ORIGINS
// ("http://localhost:5173,http://localhost:8081" — see
// sentinel-ai/app/core/config.py) so a fresh checkout works without any
// CORS configuration. VITE_API_BASE_URL overrides the API origin entirely
// for a deployed build pointing at a different host — see src/api/client.js.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
  preview: {
    port: 8081,
  },
})
