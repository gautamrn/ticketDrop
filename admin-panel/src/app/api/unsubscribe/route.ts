import { createHmac, timingSafeEqual } from "node:crypto";
import { query } from "@/lib/db";

export const runtime = "nodejs";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const user = url.searchParams.get("user") ?? "";
  const channel = url.searchParams.get("channel") ?? "";
  const signature = url.searchParams.get("sig") ?? "";
  const secret = process.env.UNSUBSCRIBE_SECRET ?? "development-only-change-me";
  const expected = createHmac("sha256", secret).update(`${user}:${channel}`).digest("hex");
  const valid =
    signature.length === expected.length &&
    timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  if (!valid) {
    return new Response("Invalid unsubscribe link.", { status: 400 });
  }
  await query(
    `UPDATE notification_channels
        SET active = false
      WHERE id = $1::uuid AND user_id = $2::uuid`,
    [channel, user],
  );
  return new Response(
    "<!doctype html><title>TicketDrop</title><p>Email alerts are disabled. You can re-enable them in settings.</p>",
    { headers: { "Content-Type": "text/html; charset=utf-8" } },
  );
}
