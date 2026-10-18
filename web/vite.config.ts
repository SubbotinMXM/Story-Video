import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// dev: python desktop.py --dev (API на 8765) + npm run dev
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: false } },
  },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1000 },
});
