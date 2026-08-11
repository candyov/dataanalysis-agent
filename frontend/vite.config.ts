import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8010',
      '/charts': 'http://127.0.0.1:8010',
      '/reports': 'http://127.0.0.1:8010',
      '/output': 'http://127.0.0.1:8010',
    },
  },
})
