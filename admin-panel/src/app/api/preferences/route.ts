import { query } from "@/lib/db";
import type { PreferenceRow } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const rows = await query<PreferenceRow>(
    `SELECT id::text, label, kind::text AS kind, weight, active,
            (embedding IS NOT NULL OR sound_signature IS NOT NULL) AS has_embedding
       FROM user_preferences
      ORDER BY kind, label`,
  );
  return Response.json({ preferences: rows });
}

/**
 * Toggle or reweight an existing preference.
 *
 * Note what is deliberately absent: creating one. A new preference needs its
 * label embedded into a 1024-d vector, and the embedder lives in the harness.
 * Reimplementing it here would put a copy of scoring behavior in a service the
 * rules file says must contain none, and a TypeScript reimplementation that
 * drifts from the Python one produces vectors that silently never match.
 * Adding goes through `python -m harness prefs add`.
 */
export async function PATCH(request: Request) {
  const body = (await request.json()) as { id?: string; active?: boolean; weight?: number };
  if (!body.id) {
    return Response.json({ error: "id is required" }, { status: 400 });
  }
  if (body.weight !== undefined && (!Number.isFinite(body.weight) || body.weight < 0)) {
    // Mirrors the CHECK constraint, so a bad value is a 400 rather than a 500.
    return Response.json({ error: "weight must be >= 0" }, { status: 400 });
  }

  const rows = await query<PreferenceRow>(
    `UPDATE user_preferences
        SET active     = COALESCE($2, active),
            weight     = COALESCE($3, weight),
            updated_at = now()
      WHERE id = $1::uuid
      RETURNING id::text, label, kind::text AS kind, weight, active,
                (embedding IS NOT NULL OR sound_signature IS NOT NULL) AS has_embedding`,
    [body.id, body.active ?? null, body.weight ?? null],
  );
  if (rows.length === 0) {
    return Response.json({ error: "no such preference" }, { status: 404 });
  }
  return Response.json(rows[0]);
}

export async function DELETE(request: Request) {
  const id = new URL(request.url).searchParams.get("id");
  if (!id) {
    return Response.json({ error: "id is required" }, { status: 400 });
  }
  await query(`DELETE FROM user_preferences WHERE id = $1::uuid`, [id]);
  return Response.json({ deleted: id });
}
