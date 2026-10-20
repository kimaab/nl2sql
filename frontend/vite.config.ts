import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 다른 PC 에서 http://<이 PC IP>:5173 로 접속할 수 있게 모든 인터페이스에서 받는다
    host: true,
    // 브라우저는 화면과 같은 주소로 /api 를 부르고, Vite 가 이 PC 의 백엔드로 넘긴다
    proxy: {
      '/api': 'http://localhost:8000',
    },
  }
})
