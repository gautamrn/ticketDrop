import { requireUser } from "@/lib/access";
import { query } from "@/lib/db";
import { rateLimit } from "@/lib/rate-limit";
import { configuredRaArea, configuredRaAreas } from "@/lib/ra-areas";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const access = await requireUser();
  if (access.response) return access.response;
  const settings = await query(
    `SELECT ra_area_id, ra_area_name, demo_mode
       FROM user_settings WHERE user_id = $1::uuid`,
    [access.user.id],
  );
  const channels = await query(
    `SELECT id::text, kind::text AS kind, destination, verified_at, active
       FROM notification_channels WHERE user_id = $1::uuid`,
    [access.user.id],
  );
  const preferences = await query(
    `SELECT id::text, label, kind::text AS kind, active
       FROM user_preferences WHERE user_id = $1::uuid
      ORDER BY kind, label`,
    [access.user.id],
  );
  return Response.json({
    settings: settings[0] ?? null,
    channels,
    preferences,
    email: access.user.email,
    areas: configuredRaAreas(),
  });
}

export async function PATCH(request: Request) {
  const access = await requireUser();
  if (access.response) return access.response;
  const limited = rateLimit(access.user.id, "settings", 10);
  if (limited) return limited;
  const body = (await request.json()) as {
    areaId?: number;
    alertEmail?: string;
  };
  const area = body.areaId === undefined ? undefined : configuredRaArea(body.areaId);
  if (!area) {
    return Response.json({ error: "areaId must be one of the configured RA_AREAS" }, { status: 400 });
  }
  if (
    body.alertEmail?.trim() &&
    body.alertEmail.trim().toLowerCase() !== access.user.email?.toLowerCase()
  ) {
    return Response.json(
      { error: "v1 alerts must use the email verified by your sign-in provider" },
      { status: 400 },
    );
  }

  await query(
    `INSERT INTO user_settings (user_id, ra_area_id, ra_area_name)
     VALUES ($1::uuid, $2, $3)
     ON CONFLICT (user_id) DO UPDATE SET
       ra_area_id = EXCLUDED.ra_area_id,
       ra_area_name = EXCLUDED.ra_area_name`,
    [access.user.id, area.id, area.name],
  );
  if (access.user.email) {
    await query(
      `INSERT INTO notification_channels (user_id, kind, destination, verified_at, active)
       VALUES ($1::uuid, 'email'::notification_channel_kind, $2, now(), true)
       ON CONFLICT (user_id, kind, destination) DO UPDATE SET active = true`,
      [access.user.id, access.user.email.toLowerCase()],
    );
  }
  return Response.json({ updated: true });
}
