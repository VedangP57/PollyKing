import { defineConfig } from "vite";
import solid from "vite-plugin-solid";

const env = (
  globalThis as { process?: { env?: Record<string, string | undefined> } }
).process?.env ?? {};

export default defineConfig(() => ({
  plugins: [solid()],
  clearScreen: false,
  server: {
    host: "localhost",
    port: 1420,
    strictPort: true,
    hmr: env.TAURI_ENV_PLATFORM
      ? {
          protocol: "ws",
          host: "localhost",
          port: 1421,
        }
      : undefined,
    watch: {
      ignored: [
        "**/src-tauri/**",
        "**/target/**",
        "**/dist/**",
        "**/data/**",
        "**/*.db",
        "**/.env*",
      ],
    },
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    target: ["es2021", "chrome105", "safari15"],
    minify: env.TAURI_ENV_DEBUG ? false : ("esbuild" as const),
    sourcemap: !!env.TAURI_ENV_DEBUG,
  },
}));
