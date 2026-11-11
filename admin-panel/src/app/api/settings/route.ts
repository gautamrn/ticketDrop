import { requireUser } from "@/lib/access";
import { query } from "@/lib/db";
import { DISCOVERY_CURRENCIES, normalizeGenres } from "@/lib/discovery";
import { rateLimit } from "@/lib/rate-limit";
import { configuredRaArea, configuredRaAreas } from "@/lib/ra-areas";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const access = await requireUser();
  if (access.response) return access.response;
  const settings = await query(
    `SELECT ra_area_id, ra_area_name, demo_mode, discovery_enabled,
            discovery_genres, discovery_max_price::text, discovery_currency
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
    discoveryEnabled?: boolean;
    discoveryGenres?: unknown;
    discoveryMaxPrice?: number | null;
    discoveryCurrency?: string;
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
  const genres =
    body.discoveryGenres === undefined ? undefined : normalizeGenres(body.discoveryGenres);
  const hasMaxPrice = Object.prototype.hasOwnProperty.call(body, "discoveryMaxPrice");
  const maxPrice = body.discoveryMaxPrice;
  if (
    hasMaxPrice &&
    maxPrice !== null &&
    (typeof maxPrice !== "number" || !Number.isFinite(maxPrice) || maxPrice < 0 || maxPrice > 10_000)
  ) {
    return Response.json({ error: "discoveryMaxPrice must be between 0 and 10000" }, { status: 400 });
  }
  if (
    body.discoveryCurrency !== undefined &&
    !DISCOVERY_CURRENCIES.includes(
      body.discoveryCurrency as (typeof DISCOVERY_CURRENCIES)[number],
    )
  ) {
    return Response.json({ error: "unsupported discoveryCurrency" }, { status: 400 });
  }

  await query(
    `INSERT INTO user_settings (
       user_id, ra_area_id, ra_area_name, discovery_enabled,
       discovery_genres, discovery_max_price, discovery_currency
     )
     VALUES (
       $1::uuid, $2, $3, COALESCE($4, false), COALESCE($5::text[], '{}'),
       $6, COALESCE($7, 'USD')
     )
     ON CONFLICT (user_id) DO UPDATE SET
       ra_area_id = EXCLUDED.ra_area_id,
       ra_area_name = EXCLUDED.ra_area_name,
       discovery_enabled = COALESCE($4, user_settings.discovery_enabled),
       discovery_genres = COALESCE($5::text[], user_settings.discovery_genres),
       discovery_max_price = CASE
         WHEN $8::boolean THEN $6
         ELSE user_settings.discovery_max_price
       END,
       discovery_currency = COALESCE($7, user_settings.discovery_currency)`,
    [
      access.user.id,
      area.id,
      area.name,
      body.discoveryEnabled ?? null,
      genres ?? null,
      hasMaxPrice ? maxPrice : null,
      body.discoveryCurrency ?? null,
      hasMaxPrice,
    ],
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
