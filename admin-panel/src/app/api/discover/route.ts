import { requireUser } from "@/lib/access";
import {
  discoveryReasons,
  normalizeGenres,
  type DiscoveryCurrency,
  type DiscoveryProfile,
} from "@/lib/discovery";
import { query } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type SettingsRow = {
  ra_area_id: number | null;
  ra_area_name: string | null;
  discovery_enabled: boolean;
  discovery_genres: string[];
  discovery_max_price: string | null;
  discovery_currency: DiscoveryCurrency;
};

type CandidateRow = {
  id: string;
  title: string;
  content_url: string | null;
  ticket_link: string | null;
  starts_at: string | null;
  cost: string | null;
  venue: string | null;
  area_name: string | null;
  genres: string[];
  artists: string[];
  watched: boolean;
  subscription_origin: "explicit" | "scout" | null;
};

export async function GET() {
  const access = await requireUser();
  if (access.response) return access.response;

  const settings = await query<SettingsRow>(
    `SELECT ra_area_id, ra_area_name, discovery_enabled, discovery_genres,
            discovery_max_price::text, discovery_currency
       FROM user_settings
      WHERE user_id = $1::uuid`,
    [access.user.id],
  );
  const row = settings[0];
  const profile: DiscoveryProfile = {
    enabled: row?.discovery_enabled ?? false,
    areaId: row?.ra_area_id ?? null,
    areaName: row?.ra_area_name ?? null,
    genres: normalizeGenres(row?.discovery_genres ?? []),
    maxPrice: row?.discovery_max_price === null || row?.discovery_max_price === undefined
      ? null
      : Number(row.discovery_max_price),
    currency: row?.discovery_currency ?? "USD",
  };

  if (!profile.enabled || !profile.areaId || profile.genres.length === 0) {
    return Response.json({ profile, events: [] });
  }

  // The catalogue is bounded before application-side cost parsing. RA exposes
  // cost as free-form text, so pretending it is a database numeric would
  // silently compare times, age limits, or multiple ticket tiers as prices.
  const candidates = await query<CandidateRow>(
    `SELECT e.id::text,
            e.title,
            e.content_url,
            e.ticket_link,
            e.starts_at,
            snapshot.cost,
            v.name AS venue,
            v.area_name,
            COALESCE(e.genres, '{}') AS genres,
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
       JOIN venues v ON v.id = e.venue_id
       LEFT JOIN LATERAL (
         SELECT cost FROM event_snapshots
          WHERE event_id = e.id ORDER BY observed_at DESC LIMIT 1
       ) snapshot ON true
       LEFT JOIN event_artists ea ON ea.event_id = e.id
       LEFT JOIN artists a ON a.id = ea.artist_id
      WHERE e.source = 'ra'
        AND v.area_id = $2
        AND (e.starts_at IS NULL OR e.starts_at >= now() - interval '1 day')
        AND EXISTS (
          SELECT 1
            FROM unnest(COALESCE(e.genres, '{}')) AS event_genre
            JOIN unnest($3::text[]) AS wanted_genre
              ON lower(event_genre) = wanted_genre
        )
      GROUP BY e.id, v.id, snapshot.cost
      ORDER BY e.starts_at NULLS LAST, e.title
      LIMIT 200`,
    [access.user.id, profile.areaId, profile.genres],
  );

  const events = candidates.flatMap((candidate) => {
    const matchReasons = discoveryReasons(candidate, profile);
    return matchReasons ? [{ ...candidate, match_reasons: matchReasons }] : [];
  });

  return Response.json({ profile, events });
}
