import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build straight into the package the FastAPI app serves, so
// `python -m job_agent dashboard` stays the single command to run everything.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "../job_agent/dashboard/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // `npm run dev` gets hot reload while talking to the real Python API.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
