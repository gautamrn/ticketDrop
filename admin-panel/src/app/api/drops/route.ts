import { query } from "@/lib/db";
import type { TransitionRow } from "@/lib/types";
import { requireOperator } from "@/lib/access";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const access = await requireOperator();
  if (access.response) return access.response;
  const url = new URL(request.url);
  const dropId = url.searchParams.get("drop_id");

  if (!dropId) {
    const drops = await query(
      `SELECT d.id::text AS drop_id, d.change::text AS change,
              d.state::text AS state, d.detected_at, d.error,
              e.title, e.content_url, e.ticket_link,
              v.name AS venue, transition.detail
         FROM drops d
         JOIN events e ON e.id = d.event_id
         LEFT JOIN venues v ON v.id = e.venue_id
         LEFT JOIN LATERAL (
           SELECT detail FROM drop_transitions
            WHERE drop_id = d.id ORDER BY at DESC LIMIT 1
         ) transition ON true
        ORDER BY d.detected_at DESC
        LIMIT 200`,
    );
    return Response.json({ drops });
  }

  const transitions = await query<TransitionRow>(
    `SELECT from_state::text, to_state::text, latency_ms, detail, at
       FROM drop_transitions
      WHERE drop_id = $1::uuid
      ORDER BY at`,
    [dropId],
  );
  return Response.json({ transitions });
}
