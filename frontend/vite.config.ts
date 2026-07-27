import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        // Pin IPv4: uvicorn binds 127.0.0.1:8481 only; `localhost` may
        // resolve to ::1 first and miss it.
        target: 'http://127.0.0.1:8481',
        changeOrigin: true,
        ws: true, // /api/task/:issue/terminal WebSocket
        headers: { 'Tailscale-User-Login': 'dev@localhost' },
      },
    },
  },
})
