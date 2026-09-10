import { query } from "@/lib/db";
import type { WatchRow } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const rows = await query<WatchRow>(
    `SELECT w.event_id::text,
            e.title,
            e.source::text        AS source,
            w.watch_source::text  AS watch_source,
            w.tier::text          AS tier,
            w.active,
            w.reason,
            w.last_polled_at,
            e.starts_at,
            -- DueWatchItems filters on watch_source = $1 AND e.source = $1,
            -- where $1 is the availability adapter's own name. Only 'ra' polls
            -- availability, so a row naming any other source is inert until
            -- the harness repoint loop follows event_links onto the RA
            -- counterpart (WATCHLIST_REPOINT_INTERVAL_MS, default 30s).
            -- Showing the counterpart distinguishes "waiting on a link" from
            -- "waiting on the heal".
            (w.watch_source = 'ra' AND e.source = 'ra') AS pollable,
            -- If the resolver has since matched this event to an RA listing,
            -- the inert row is one repointing away from being live. Showing the
            -- counterpart turns "this is broken" into "this is the fix".
            (SELECT c.title FROM event_links l
               JOIN events c ON c.id = l.canonical_event_id
              WHERE l.event_id = w.event_id AND c.source = 'ra'
              LIMIT 1) AS resolved_to
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
