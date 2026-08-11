import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发模式下把 /api 转发到后端（生产环境由 FastAPI 同端口托管前端构建产物，
// 不需要这层代理，也不需要 CORS）。
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": {
        target: process.env.ZLIB_WEB_BACKEND || "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
});
