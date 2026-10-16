import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发期把控制面请求代理到本机 API，避免前端持有任何跨域或凭据配置。
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
})
