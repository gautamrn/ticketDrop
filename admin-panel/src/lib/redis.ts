import Redis from "ioredis";

// Next dev recompiles modules on every edit. Without stashing the client on the
// global object each recompile would open another connection and leak them until
// Redis refuses new ones.
const globalForRedis = globalThis as unknown as { redis?: Redis };

export function redis(): Redis {
  if (!globalForRedis.redis) {
    globalForRedis.redis = new Redis(process.env.REDIS_URL ?? "redis://localhost:6379", {
      // The panel is a read model. If Redis is down the correct behavior is a
      // degraded page, not a request that hangs until the browser gives up.
      maxRetriesPerRequest: 2,
      connectTimeout: 3000,
      lazyConnect: false,
    });
    globalForRedis.redis.on("error", (err) => {
      console.error("[redis]", err.message);
    });
  }
  return globalForRedis.redis;
}

/**
 * A dedicated connection for blocking reads.
 *
 * `XREAD BLOCK` occupies its connection for the duration of the block, so
 * sharing the pooled client would stall every other query behind a tail that is
 * idle by design.
 */
export function blockingRedis(): Redis {
  return new Redis(process.env.REDIS_URL ?? "redis://localhost:6379", {
    maxRetriesPerRequest: null,
    connectTimeout: 3000,
  });
}

export const STREAM_DROPS = "ticket:drops";
export const STREAM_RESULTS = "ticket:drops:results";
export const CONSUMER_GROUP = "tdrop-harness";

/** Redis stream entries are flat [field, value, field, value, ...] arrays. */
export function fieldsToObject(fields: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (let i = 0; i + 1 < fields.length; i += 2) {
    out[fields[i]] = fields[i + 1];
  }
  return out;
}
