import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// citizen-service already allows http://localhost:3000 in its CORS config
// (see citizen-service/app/main.py), so the dev server intentionally runs
// on port 3000 to match without needing a proxy for local development.
// VITE_API_BASE_URL can override the API origin entirely (e.g. for a
// deployed build pointing at a different host) — see src/api/client.js.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
  },
  preview: {
    port: 3000,
  },
})
