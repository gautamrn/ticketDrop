import { query } from "@/lib/db";
import type { LinkRow } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Cross-source matches awaiting human judgement.
 *
 * ARCHITECTURE.md §9 failure 9: low-confidence matches surface here rather than
 * auto-merging. The resolver already refuses anything ambiguous, so what lands
 * in this queue is what it accepted but is least sure about — the band where a
 * human is genuinely better than a threshold.
 */
export async function GET() {
  const rows = await query<LinkRow>(
    `SELECT l.event_id::text,
            l.canonical_event_id::text,
            l.match_confidence,
            l.match_method,
            l.confirmed,
            a.title         AS from_title,
            a.source::text  AS from_source,
            a.starts_at     AS from_starts_at,
            b.title         AS to_title,
            b.source::text  AS to_source,
            b.starts_at     AS to_starts_at
       FROM event_links l
       JOIN events a ON a.id = l.event_id
       JOIN events b ON b.id = l.canonical_event_id
      WHERE NOT l.confirmed
      ORDER BY l.match_confidence NULLS FIRST
      LIMIT 100`,
  );
  return Response.json({ links: rows });
}

export async function POST(request: Request) {
  const body = (await request.json()) as { event_id?: string; confirm?: boolean };
  if (!body.event_id || typeof body.confirm !== "boolean") {
    return Response.json({ error: "event_id and confirm are required" }, { status: 400 });
  }

  if (body.confirm) {
    const rows = await query<{ event_id: string }>(
      `UPDATE event_links SET confirmed = true
        WHERE event_id = $1::uuid
        RETURNING event_id::text`,
      [body.event_id],
    );
    if (rows.length === 0) {
      return Response.json({ error: "no such link" }, { status: 404 });
    }
    return Response.json({ event_id: rows[0].event_id, confirmed: true });
  }

  // Rejecting deletes the row. The resolver only reconsiders events that have no
  // link, so leaving a rejected row in place would permanently block the event
  // from ever being matched correctly.
  await query(`DELETE FROM event_links WHERE event_id = $1::uuid`, [body.event_id]);
  return Response.json({ event_id: body.event_id, confirmed: false, deleted: true });
}
