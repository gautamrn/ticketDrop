import { config as loadEnv } from "dotenv";
import path from "node:path";
import type { NextConfig } from "next";

// The panel reads the same Postgres and Redis as the rest of the stack, so it
// reads the same .env rather than keeping a second copy of the credentials in
// admin-panel/.env.local. `override: false` means a real environment variable
// always wins, which is what makes this safe in a container.
loadEnv({ path: path.resolve(process.cwd(), "..", ".env"), override: false });

const nextConfig: NextConfig = {
  // ioredis and pg open sockets and must not be bundled into the edge runtime.
  serverExternalPackages: ["ioredis", "pg"],
  reactStrictMode: true,
};

export default nextConfig;
