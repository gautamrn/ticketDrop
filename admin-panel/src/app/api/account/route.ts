import { requireUser } from "@/lib/access";
import { query } from "@/lib/db";
import { rateLimit } from "@/lib/rate-limit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const access = await requireUser();
  if (access.response) return access.response;
  const [settings, preferences, subscriptions, deliveries] = await Promise.all([
    query("SELECT * FROM user_settings WHERE user_id = $1::uuid", [access.user.id]),
    query(
      `SELECT label, kind::text AS kind, active, created_at
         FROM user_preferences WHERE user_id = $1::uuid`,
      [access.user.id],
    ),
    query(
      `SELECT e.title, e.content_url, e.ticket_link, s.active,
              s.origin::text AS origin, s.user_disabled, s.created_at
         FROM event_subscriptions s JOIN events e ON e.id = s.event_id
        WHERE s.user_id = $1::uuid`,
      [access.user.id],
    ),
    query(
      `SELECT d.change::text AS change, nd.status::text AS status,
              nd.sent_at, nd.created_at
         FROM notification_deliveries nd JOIN drops d ON d.id = nd.drop_id
        WHERE nd.user_id = $1::uuid`,
      [access.user.id],
    ),
  ]);
  return Response.json({
    exportedAt: new Date().toISOString(),
    account: { name: access.user.name, email: access.user.email },
    settings,
    preferences,
    subscriptions,
    deliveries,
  });
}

export async function DELETE() {
  const access = await requireUser();
  if (access.response) return access.response;
  const limited = rateLimit(access.user.id, "account-delete", 1, 60 * 60 * 1000);
  if (limited) return limited;
  await query("DELETE FROM users WHERE id = $1::uuid", [access.user.id]);
  return Response.json({ deleted: true });
}
