import { query } from "@/lib/db";
import { CONSUMER_GROUP, redis, STREAM_DROPS, STREAM_RESULTS } from "@/lib/redis";
import type { Health } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type GroupInfo = { name: string; consumers: number; pending: number; lag: number | null };

export async function GET() {
  const health: Health = {
    redis: false,
    postgres: false,
    streams: [],
    lag: null,
    pending: null,
    consumers: 0,
    counts: {},
    soundScoringAvailable: false,
    activePreferences: 0,
  };

  // Redis and Postgres are probed independently so one being down still renders
  // the half of the dashboard that works. A panel that goes fully blank during
  // an incident is a panel you cannot use during an incident.
  try {
    const r = redis();
    for (const name of [STREAM_DROPS, STREAM_RESULTS]) {
      health.streams.push({ name, length: await r.xlen(name) });
    }

    const groups = (await r.xinfo("GROUPS", STREAM_DROPS).catch(() => [])) as unknown[];
    for (const raw of groups) {
      const info = parseGroup(raw as unknown[]);
      if (info?.name === CONSUMER_GROUP) {
        health.lag = info.lag;
        health.pending = info.pending;
        health.consumers = info.consumers;
      }
    }
    health.redis = true;
  } catch {
    health.redis = false;
  }

  try {
    const [row] = await query<{
      notified: string;
      skipped: string;
      failed: string;
      in_flight: string;
      watchlist: string;
      pending_links: string;
      prefs: string;
      sound_ready: boolean;
    }>(`
      SELECT
        (SELECT count(*) FROM drops WHERE state = 'NOTIFIED')               AS notified,
        (SELECT count(*) FROM drops WHERE state = 'SKIPPED')                AS skipped,
        (SELECT count(*) FROM drops WHERE state = 'FAILED')                 AS failed,
        (SELECT count(*) FROM drops
          WHERE state NOT IN ('NOTIFIED','SKIPPED','FAILED'))               AS in_flight,
        (SELECT count(*) FROM watchlist WHERE active)                       AS watchlist,
        (SELECT count(*) FROM event_links WHERE NOT confirmed)              AS pending_links,
        (SELECT count(*) FROM user_preferences WHERE active)                AS prefs,
        (SELECT EXISTS (SELECT 1 FROM artists WHERE sound_signature IS NOT NULL)
            AND EXISTS (SELECT 1 FROM user_preferences
                        WHERE active AND kind = 'sound'::preference_kind
                          AND sound_signature IS NOT NULL))                 AS sound_ready
    `);

    health.counts = {
      notified: Number(row.notified),
      skipped: Number(row.skipped),
      failed: Number(row.failed),
      inFlight: Number(row.in_flight),
      watchlist: Number(row.watchlist),
      pendingLinks: Number(row.pending_links),
    };
    health.activePreferences = Number(row.prefs);
    health.soundScoringAvailable = row.sound_ready;
    health.postgres = true;
  } catch {
    health.postgres = false;
  }

  return Response.json(health);
}

/** XINFO GROUPS returns a flat key/value array, and its field order is not
 *  guaranteed across Redis versions, so read it by name rather than by index. */
function parseGroup(raw: unknown[]): GroupInfo | null {
  if (!Array.isArray(raw)) return null;
  const map: Record<string, unknown> = {};
  for (let i = 0; i + 1 < raw.length; i += 2) {
    map[String(raw[i])] = raw[i + 1];
  }
  return {
    name: String(map.name ?? ""),
    consumers: Number(map.consumers ?? 0),
    pending: Number(map.pending ?? 0),
    lag: map.lag === null || map.lag === undefined ? null : Number(map.lag),
  };
}
