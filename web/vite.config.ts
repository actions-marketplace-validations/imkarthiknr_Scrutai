/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The production bundle ships inside the Python package, so `pip install scrutai[web]`
// serves the UI without Node. `npm run dev` proxies the API to a local `scrutai serve`.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../src/scrutai/web/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 600,
  },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
  test: { environment: "node" },
});
