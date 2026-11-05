import { buildApp } from "./app.js";
const app = buildApp();
await app.listen({ port: Number(process.env.PORT ?? 3001), host: "127.0.0.1" });
console.log(
  `Office API: http://127.0.0.1:${process.env.PORT ?? 3001} (local-only; simulated supplier)`,
);
for (const signal of ["SIGINT", "SIGTERM"] as const)
  process.on(signal, () => {
    void app.close().then(() => process.exit(0));
  });
