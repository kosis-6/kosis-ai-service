import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const API = "http://127.0.0.1:8000";

export default defineConfig({
  base: "/chat/",
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      ["/auth", "/search", "/ask", "/conversations", "/admin", "/health", "/docs", "/openapi.json"].map((path) => [
        path,
        API,
      ]),
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
