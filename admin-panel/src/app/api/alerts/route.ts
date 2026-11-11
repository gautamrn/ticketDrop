import { requireUser } from "@/lib/access";
import { query } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const access = await requireUser();
  if (access.response) return access.response;
  const rows = await query(
    `SELECT d.id::text AS drop_id,
            d.change::text AS change,
            d.detected_at,
            d.state::text AS state,
            e.title,
            e.starts_at,
            e.content_url,
            e.ticket_link,
            v.name AS venue,
            nd.status::text AS delivery_status,
            nd.sent_at,
            nd.last_error,
            transition.detail
       FROM notification_deliveries nd
       JOIN drops d ON d.id = nd.drop_id
       JOIN events e ON e.id = d.event_id
       LEFT JOIN venues v ON v.id = e.venue_id
       LEFT JOIN LATERAL (
         SELECT detail
           FROM drop_transitions
          WHERE drop_id = d.id
          ORDER BY at DESC
          LIMIT 1
       ) transition ON true
      WHERE nd.user_id = $1::uuid
      ORDER BY d.detected_at DESC
      LIMIT 100`,
    [access.user.id],
  );
  return Response.json({ alerts: rows });
}
