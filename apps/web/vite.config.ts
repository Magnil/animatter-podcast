import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is a separate local process. Proxying keeps the frontend free of
// hard-coded ports and sidesteps CORS for the SSE/Range endpoints.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8317",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      // `harness.html` is a development-only entry: it mounts the transcript pane
      // with hand-authored data so every visual state (open bubble, speaker menu,
      // multi-sentence selection) can be screenshotted. Shipping it would put a
      // dead page in `dist`, so only the real entry is built.
      input: { index: "index.html" },
    },
  },
});
