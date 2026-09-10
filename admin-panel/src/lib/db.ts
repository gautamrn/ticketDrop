import { Pool } from "pg";

const globalForPg = globalThis as unknown as { pgPool?: Pool };

export function db(): Pool {
  if (!globalForPg.pgPool) {
    globalForPg.pgPool = new Pool({
      connectionString:
        process.env.DATABASE_URL ?? "postgres://ticketdrop:ticketdrop@localhost:5432/ticketdrop",
      // Small: the panel is one operator's dashboard, and every connection here
      // is one the ingest service or harness cannot have.
      max: 4,
      connectionTimeoutMillis: 3000,
      idleTimeoutMillis: 30_000,
    });
    globalForPg.pgPool.on("error", (err) => {
      console.error("[pg]", err.message);
    });
  }
  return globalForPg.pgPool;
}

export async function query<T>(text: string, params: unknown[] = []): Promise<T[]> {
  const result = await db().query(text, params);
  return result.rows as T[];
}
