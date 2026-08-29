import react from "@vitejs/plugin-react";
// defineConfig do vitest/config, e não do vite: é o mesmo objeto de configuração com o
// bloco `test` tipado junto, em vez de um cast para o Vite aceitar o que não conhece.
import { defineConfig } from "vitest/config";

// O dashboard é estático: build vira arquivos, e não um runtime Node para operar. A API
// já é o control plane, e cada peça a mais de superfície operacional é cobrada do mesmo
// desenvolvedor solo (R-11).

export default defineConfig({
  server: {
    // Em desenvolvimento o /v1 vai para a API por proxy, e não por URL absoluta. Assim o
    // código do cliente monta caminho relativo em todo ambiente -- nada de uma variável
    // de build que, esquecida, aponta o dashboard de produção para o localhost de alguém.
    proxy: {
      "/v1": { target: "http://localhost:3000", changeOrigin: true },
    },
  },
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.ts", "test/**/*.test.tsx"],
  },
});
