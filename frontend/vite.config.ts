import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 화면은 FastAPI 가 /chat 으로 서빙한다 (backend/app/main.py 가 dist/ 를 읽는다).
// npm run dev (5173) 로 띄우면 API 요청은 FastAPI(8000) 로 넘긴다:
// 브라우저가 보기에는 같은 주소라 CORS 가 필요 없고 Refresh Token 쿠키(Path=/auth)도 그대로 실린다
const API = "http://127.0.0.1:8000";

export default defineConfig({
  base: "/chat/",
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      ["/auth", "/search", "/ask", "/admin", "/health", "/docs", "/openapi.json"].map((path) => [path, API]),
    ),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    environment: "node",
  },
});
