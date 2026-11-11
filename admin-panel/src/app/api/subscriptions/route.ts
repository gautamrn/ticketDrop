import { requireUser } from "@/lib/access";
import { db, query } from "@/lib/db";
import { rateLimit } from "@/lib/rate-limit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const access = await requireUser();
  if (access.response) return access.response;
  const rows = await query(
    `SELECT s.event_id::text, s.active, s.origin::text AS origin,
            s.user_disabled, s.created_at,
            e.title, e.starts_at, e.content_url, v.name AS venue
       FROM event_subscriptions s
       JOIN events e ON e.id = s.event_id
       LEFT JOIN venues v ON v.id = e.venue_id
      WHERE s.user_id = $1::uuid
      ORDER BY s.active DESC, e.starts_at NULLS LAST`,
    [access.user.id],
  );
  return Response.json({ subscriptions: rows });
}

export async function POST(request: Request) {
  const access = await requireUser();
  if (access.response) return access.response;
  const limited = rateLimit(access.user.id, "subscriptions");
  if (limited) return limited;
  const body = (await request.json()) as { eventId?: string };
  if (!body.eventId) {
    return Response.json({ error: "eventId is required" }, { status: 400 });
  }

  const client = await db().connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", [access.user.id]);
    const event = await client.query<{ id: string }>(
      "SELECT id::text FROM events WHERE id = $1::uuid AND source = 'ra'",
      [body.eventId],
    );
    if (!event.rows[0]) {
      await client.query("ROLLBACK");
      return Response.json({ error: "event not found" }, { status: 404 });
    }
    const existing = await client.query<{ active: boolean }>(
      `SELECT active FROM event_subscriptions
        WHERE user_id = $1::uuid AND event_id = $2::uuid`,
      [access.user.id, body.eventId],
    );
    const [{ count }] = (
      await client.query<{ count: number }>(
        `SELECT count(*)::int AS count FROM event_subscriptions
          WHERE user_id = $1::uuid AND active`,
        [access.user.id],
      )
    ).rows;
    if (!existing.rows[0]?.active && count >= 25) {
      await client.query("ROLLBACK");
      return Response.json({ error: "25-watch limit reached" }, { status: 409 });
    }
    const rows = await client.query<{ event_id: string; active: boolean; origin: string }>(
      `INSERT INTO event_subscriptions (user_id, event_id, active, origin, user_disabled)
       VALUES ($1::uuid, $2::uuid, true, 'explicit'::subscription_origin, false)
       ON CONFLICT (user_id, event_id) DO UPDATE SET
         active = true, origin = 'explicit'::subscription_origin, user_disabled = false
       RETURNING event_id::text, active, origin::text`,
      [access.user.id, body.eventId],
    );
    await client.query("COMMIT");
    return Response.json(rows.rows[0], { status: 201 });
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function DELETE(request: Request) {
  const access = await requireUser();
  if (access.response) return access.response;
  const limited = rateLimit(access.user.id, "subscriptions");
  if (limited) return limited;
  const eventId = new URL(request.url).searchParams.get("eventId");
  if (!eventId) {
    return Response.json({ error: "eventId is required" }, { status: 400 });
  }
  await query(
    `UPDATE event_subscriptions SET active = false, user_disabled = true
      WHERE user_id = $1::uuid AND event_id = $2::uuid`,
    [access.user.id, eventId],
  );
  return Response.json({ eventId, active: false });
}
