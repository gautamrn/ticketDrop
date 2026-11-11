import { auth } from "@/auth";
import { EventBrowser } from "@/components/EventBrowser";
import { query } from "@/lib/db";

export default async function EventsPage() {
  const session = await auth();
  const rows = session?.user.id
    ? await query<{ ra_area_id: number | null }>(
        "SELECT ra_area_id FROM user_settings WHERE user_id = $1::uuid",
        [session.user.id],
      )
    : [];
  return <EventBrowser initialArea={rows[0]?.ra_area_id} />;
}
