import { query } from "@/lib/db";
import type { WatchRow } from "@/lib/types";
import { requireOperator } from "@/lib/access";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const access = await requireOperator();
  if (access.response) return access.response;
  const rows = await query<WatchRow>(
    `SELECT w.event_id::text,
            e.title,
            e.source::text        AS source,
            w.tier::text          AS tier,
            w.active,
            w.reason,
            w.last_polled_at,
            e.starts_at
       FROM watchlist w
       JOIN events e ON e.id = w.event_id
      ORDER BY w.active DESC, w.last_polled_at NULLS FIRST
      LIMIT 200`,
  );
  return Response.json({ watchlist: rows });
}

/**
 * The operator's kill switch for a single event.
 *
 * Deactivating rather than deleting: the row records why the event was promoted
 * in the first place, and an operator silencing a noisy event should not also
 * erase the evidence of the decision that put it there.
 */
export async function POST(request: Request) {
  const access = await requireOperator();
  if (access.response) return access.response;
  const body = (await request.json()) as { event_id?: string; active?: boolean };
  if (!body.event_id || typeof body.active !== "boolean") {
    return Response.json({ error: "event_id and active are required" }, { status: 400 });
  }

  const rows = await query<{ event_id: string; active: boolean }>(
    `UPDATE watchlist
        SET active = $2
      WHERE event_id = $1::uuid
      RETURNING event_id::text, active`,
    [body.event_id, body.active],
  );
  if (rows.length === 0) {
    return Response.json({ error: "not on the watchlist" }, { status: 404 });
  }
  return Response.json(rows[0]);
}
