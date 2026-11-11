import { requireUser } from "@/lib/access";
import { query } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const access = await requireUser();
  if (access.response) return access.response;

  const url = new URL(request.url);
  const search = url.searchParams.get("q")?.trim() ?? "";
  const area = Number(url.searchParams.get("area") ?? 0);
  const offset = Math.max(0, Number(url.searchParams.get("offset") ?? 0) || 0);

  const rows = await query(
    `SELECT e.id::text,
            e.title,
            e.content_url,
            e.ticket_link,
            e.starts_at,
            snapshot.cost,
            e.is_ticketed,
            e.attending,
            v.name AS venue,
            v.area_id,
            v.area_name,
            COALESCE(array_agg(a.name ORDER BY ea.billing)
              FILTER (WHERE a.id IS NOT NULL), '{}') AS artists,
            EXISTS (
              SELECT 1 FROM event_subscriptions s
               WHERE s.event_id = e.id AND s.user_id = $1::uuid AND s.active
            ) AS watched,
            (
              SELECT s.origin::text FROM event_subscriptions s
               WHERE s.event_id = e.id AND s.user_id = $1::uuid AND s.active
            ) AS subscription_origin
       FROM events e
       LEFT JOIN venues v ON v.id = e.venue_id
       LEFT JOIN LATERAL (
         SELECT cost FROM event_snapshots
          WHERE event_id = e.id ORDER BY observed_at DESC LIMIT 1
       ) snapshot ON true
       LEFT JOIN event_artists ea ON ea.event_id = e.id
       LEFT JOIN artists a ON a.id = ea.artist_id
      WHERE e.source = 'ra'
        AND (e.starts_at IS NULL OR e.starts_at >= now() - interval '1 day')
        AND ($2::int = 0 OR v.area_id = $2)
        AND (
          $3 = ''
          OR e.title ILIKE '%' || $3 || '%'
          OR v.name ILIKE '%' || $3 || '%'
          OR EXISTS (
            SELECT 1 FROM event_artists sea
            JOIN artists sa ON sa.id = sea.artist_id
            WHERE sea.event_id = e.id AND sa.name ILIKE '%' || $3 || '%'
          )
        )
      GROUP BY e.id, v.id, snapshot.cost
      ORDER BY e.starts_at NULLS LAST, e.title
      LIMIT 50 OFFSET $4`,
    [access.user.id, area, search, offset],
  );

  return Response.json({ events: rows, nextOffset: rows.length === 50 ? offset + 50 : null });
}
