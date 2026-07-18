import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
      "^/node_modules/\\.vite/deps/.*\\.mjs(\\?.*)?$": {
        target: "http://localhost:5173",
        rewrite: (path) => {
          const filename = path.split('?')[0].split('/').pop();
          return `/${filename}`;
        }
      }
    },
  },
});
