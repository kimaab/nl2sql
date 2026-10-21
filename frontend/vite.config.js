import { existsSync, readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// Superset 과 같은 자체 서명 인증서를 쓴다 (superset/make_cert.py 가 만든다).
// 인증서에 든 접속 주소는 superset/.env 의 SUPERSET_TLS_HOSTS — 이 PC 의 주소라 화면에도 그대로 맞는다.
const CERT = '../superset/certs/server.crt';
const KEY = '../superset/certs/server.key';
const https = existsSync(CERT) && existsSync(KEY) ? { cert: readFileSync(CERT), key: readFileSync(KEY) } : undefined;
if (!https)
    console.warn(`[vite] ${CERT} 가 없어 http 로 띄웁니다 (superset 의 docker compose up 이 만든다)`);
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        // 다른 PC 에서 https://<이 PC IP>:5173 로 접속할 수 있게 모든 인터페이스에서 받는다
        host: true,
        https,
        // 브라우저는 화면과 같은 주소로 /api 를 부르고, Vite 가 이 PC 의 백엔드로 넘긴다.
        // 백엔드까지는 이 PC 안에서만 오가므로 http 로 둔다.
        proxy: {
            '/api': 'http://localhost:8000',
        },
    }
});
