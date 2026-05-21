import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";

export default defineConfig({
  plugins: [
    // Skip `*.test.tsx` files under src/routes/ — TSR already excludes
    // them from the route tree but emits a noisy warning on each dev
    // start. Without the pattern, future test files alongside route
    // components would trigger it again.
    TanStackRouterVite({ routeFileIgnorePattern: "\\.test\\." }),
    react(),
    tailwindcss(),
  ],
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:47823",
        changeOrigin: false,
        ws: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test-setup.ts",
    css: false,
  },
});
