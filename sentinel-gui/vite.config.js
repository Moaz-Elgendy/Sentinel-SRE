import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const rootDir = path.dirname(fileURLToPath(import.meta.url))

// Ports here match sentinel-ai's default SENTINEL_GUI_ORIGINS
// ("http://localhost:5173,http://localhost:8081" — see
// sentinel-ai/app/core/config.py) so a fresh checkout works without any
// CORS configuration. VITE_API_BASE_URL overrides the API origin entirely
// for a deployed build pointing at a different host — see src/api/client.js.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // `@/…` is the shadcn/ui import convention (see components.json).
    alias: { '@': path.resolve(rootDir, 'src') },
  },
  server: {
    port: 5173,
  },
  preview: {
    port: 8081,
  },
})
