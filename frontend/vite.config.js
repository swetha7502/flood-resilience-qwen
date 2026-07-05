import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const backendUrl = env.VITE_BACKEND_URL || 'http://localhost:8000';

  return {
    plugins: [react()],
    server: {
      port: 3000,
      // Proxy REST calls so CORS is never an issue in dev.
      proxy: {
        '/approve': backendUrl,
        '/cloud': backendUrl,
        '/health': backendUrl,
      },
    },
  };
});
