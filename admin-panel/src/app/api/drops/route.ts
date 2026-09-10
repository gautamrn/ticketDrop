import { query } from "@/lib/db";
import type { TransitionRow } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * The durable view of the FSM, from Postgres rather than the results stream.
 *
 * The stream is capped by MAXLEN and is the live feed; this is the record. They
 * are deliberately separate reads: an operator arriving after a trimmed entry
 * still needs to see what happened.
 */
export async function GET(request: Request) {
  const url = new URL(request.url);
  const dropId = url.searchParams.get("drop_id");

  if (!dropId) {
    return Response.json({ error: "drop_id is required" }, { status: 400 });
  }

  const transitions = await query<TransitionRow>(
    `SELECT from_state::text, to_state::text, latency_ms, detail, at
       FROM drop_transitions
      WHERE drop_id = $1::uuid
      ORDER BY at`,
    [dropId],
  );
  return Response.json({ transitions });
}
