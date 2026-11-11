import { query } from "@/lib/db";
import type { PreferenceRow } from "@/lib/types";
import { requireUser } from "@/lib/access";
import { rateLimit } from "@/lib/rate-limit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const access = await requireUser();
  if (access.response) return access.response;
  const rows = await query<PreferenceRow>(
    `SELECT id::text, label, kind::text AS kind, active
       FROM user_preferences
      WHERE user_id = $1::uuid
      ORDER BY kind, label`,
    [access.user.id],
  );
  return Response.json({ preferences: rows });
}

export async function POST(request: Request) {
  const access = await requireUser();
  if (access.response) return access.response;
  const limited = rateLimit(access.user.id, "preferences", 10);
  if (limited) return limited;
  const body = (await request.json()) as { label?: string; kind?: string };
  const label = body.label?.trim();
  const kind = body.kind ?? "artist";
  if (!label || !["artist", "venue"].includes(kind)) {
    return Response.json({ error: "valid label and kind are required" }, { status: 400 });
  }
  const rows = await query<PreferenceRow>(
    `INSERT INTO user_preferences (user_id, label, kind)
     VALUES ($1::uuid, $2, $3::preference_kind)
     ON CONFLICT (user_id, kind, (lower(label))) DO UPDATE SET
       label = EXCLUDED.label, active = true
     RETURNING id::text, label, kind::text AS kind, active`,
    [access.user.id, label, kind],
  );
  return Response.json(rows[0], { status: 201 });
}

export async function PATCH(request: Request) {
  const access = await requireUser();
  if (access.response) return access.response;
  const body = (await request.json()) as { id?: string; active?: boolean };
  if (!body.id) {
    return Response.json({ error: "id is required" }, { status: 400 });
  }
  const rows = await query<PreferenceRow>(
    `UPDATE user_preferences
        SET active     = COALESCE($2, active),
            updated_at = now()
      WHERE id = $1::uuid AND user_id = $3::uuid
      RETURNING id::text, label, kind::text AS kind, active`,
    [body.id, body.active ?? null, access.user.id],
  );
  if (rows.length === 0) {
    return Response.json({ error: "no such preference" }, { status: 404 });
  }
  return Response.json(rows[0]);
}

export async function DELETE(request: Request) {
  const access = await requireUser();
  if (access.response) return access.response;
  const id = new URL(request.url).searchParams.get("id");
  if (!id) {
    return Response.json({ error: "id is required" }, { status: 400 });
  }
  await query(`DELETE FROM user_preferences WHERE id = $1::uuid AND user_id = $2::uuid`, [
    id,
    access.user.id,
  ]);
  return Response.json({ deleted: id });
}
