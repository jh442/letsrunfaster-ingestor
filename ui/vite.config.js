import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  // Relative asset URLs so the build works both when served by the local
  // FastAPI server and when loaded straight off disk (Electron dev).
  base: './',
  build: {
    // Ship into the Python package: PyInstaller's `datas` already bundles
    // garmin_engine/static, so there is no copy step to forget.
    outDir: path.resolve(here, '../garmin_engine/static'),
    emptyOutDir: true,
    // Local-only app: cache-busting hashes buy nothing and make support
    // ("which file is that?") and tests harder. Stable names instead.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
  server: {
    // `npm run dev` against a running `garmin-ingestor ui --no-browser`.
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
});
