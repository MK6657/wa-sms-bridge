import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: path.resolve(__dirname, "../app/web_dist"),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom"],
          icons: ["lucide-react"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:9090",
        changeOrigin: true,
        headers: {
          Origin: "http://127.0.0.1:9090",
          Referer: "http://127.0.0.1:9090/",
        },
      },
      "/docs": {
        target: "http://127.0.0.1:9090",
        changeOrigin: true,
      },
      "/openapi.json": {
        target: "http://127.0.0.1:9090",
        changeOrigin: true,
      },
    },
  },
});
