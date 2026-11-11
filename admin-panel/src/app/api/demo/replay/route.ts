import { randomUUID } from "node:crypto";
import { requireUser } from "@/lib/access";
import { db } from "@/lib/db";
import { rateLimit } from "@/lib/rate-limit";

export const runtime = "nodejs";

export async function POST() {
  if (process.env.PUBLIC_DEMO_MODE !== "true") {
    return Response.json({ error: "demo replay is disabled" }, { status: 404 });
  }
  const access = await requireUser();
  if (access.response) return access.response;
  const limited = rateLimit(access.user.id, "demo-replay", 3, 60 * 60 * 1000);
  if (limited) return limited;

  const client = await db().connect();
  const dropId = randomUUID();
  try {
    await client.query("BEGIN");
    const event = await client.query<{ id: string }>(
      `INSERT INTO events (
         source, source_event_id, title, content_url, ticket_link, starts_at,
         is_ticketed, raw
       ) VALUES (
         'ra', 'demo-warehouse-session',
         '[Demo] Warehouse Session', 'https://ra.co/events',
         'https://tickets.example/demo', now() + interval '7 days',
         true, '{"demo":true}'::jsonb
       )
       ON CONFLICT (source, source_event_id) DO UPDATE SET
         title = EXCLUDED.title, raw = EXCLUDED.raw
       RETURNING id::text`,
    );
    const eventId = event.rows[0].id;
    await client.query(
      `INSERT INTO drops (
         id, event_id, change, fingerprint, state, detected_at, notified_at
       ) VALUES (
         $1::uuid, $2::uuid, 'tier_available'::change_kind,
         $3, 'NOTIFIED'::drop_state, now(), now()
       )`,
      [dropId, eventId, `demo-${dropId}`],
    );
    await client.query(
      `INSERT INTO drop_transitions (drop_id, from_state, to_state, latency_ms, detail)
       VALUES ($1::uuid, NULL, 'NOTIFIED'::drop_state, 36,
         '{"demo":true,"change":"presale opened"}'::jsonb)`,
      [dropId],
    );
    if (access.user.email) {
      const channel = await client.query<{ id: string }>(
        `INSERT INTO notification_channels (
           user_id, kind, destination, verified_at, active
         ) VALUES (
           $1::uuid, 'email'::notification_channel_kind, $2, now(), true
         )
         ON CONFLICT (user_id, kind, destination) DO UPDATE SET active = true
         RETURNING id::text`,
        [access.user.id, access.user.email],
      );
      await client.query(
        `INSERT INTO notification_deliveries (
           drop_id, user_id, channel_id, channel, status,
           attempts, provider_message_id, sent_at
         ) VALUES (
           $1::uuid, $2::uuid, $3::uuid, 'email'::notification_channel_kind,
           'sent'::delivery_status, 1, 'demo-replay', now()
         )`,
        [dropId, access.user.id, channel.rows[0].id],
      );
    }
    await client.query("COMMIT");
    return Response.json({ dropId, demo: true }, { status: 201 });
  } catch (error) {
    await client.query("ROLLBACK");
    console.error("[demo replay]", error);
    return Response.json({ error: "could not create demo replay" }, { status: 500 });
  } finally {
    client.release();
  }
}
