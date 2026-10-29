import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Monaco is one ~3.6 MB chunk (lazy-loaded); the rest of the app is small.
  build: { chunkSizeWarningLimit: 4000 },
  server: {
    // Forward API calls (and the progress WebSocket) to the FastAPI backend.
    proxy: {
      "/api": { target: "http://localhost:8000", ws: true },
    },
  },
});
