import Link from "next/link";
import { auth } from "@/auth";
import { query } from "@/lib/db";
import { DemoReplayButton } from "@/components/DemoReplayButton";

export default async function AppHome() {
  const session = await auth();
  const userId = session!.user.id;
  const [stats] = await query<{
    watches: number;
    alerts: number;
    sent: number;
  }>(
    `SELECT
       (SELECT count(*)::int FROM event_subscriptions
         WHERE user_id = $1::uuid AND active) AS watches,
       (SELECT count(*)::int FROM notification_deliveries
         WHERE user_id = $1::uuid) AS alerts,
       (SELECT count(*)::int FROM notification_deliveries
         WHERE user_id = $1::uuid AND status = 'sent') AS sent`,
    [userId],
  );

  const recent = await query<{
    drop_id: string;
    title: string;
    change: string;
    detected_at: string;
  }>(
    `SELECT d.id::text AS drop_id, e.title, d.change::text AS change,
            d.detected_at
       FROM notification_deliveries nd
       JOIN drops d ON d.id = nd.drop_id
       JOIN events e ON e.id = d.event_id
      WHERE nd.user_id = $1::uuid
      ORDER BY d.detected_at DESC LIMIT 5`,
    [userId],
  );

  return (
    <section className="py-8">
      <p className="text-sm text-ink-400">Welcome back, {session!.user.name ?? "listener"}.</p>
      <h1 className="mt-1 text-3xl font-semibold">Your watch desk</h1>
      <div className="mt-6 grid gap-3 sm:grid-cols-3">
        {[
          ["Active watches", stats?.watches ?? 0],
          ["Onsale signals", stats?.alerts ?? 0],
          ["Emails sent", stats?.sent ?? 0],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg border border-ink-800 bg-ink-900 p-4">
            <p className="text-2xl font-semibold">{value}</p>
            <p className="mt-1 text-xs text-ink-400">{label}</p>
          </div>
        ))}
      </div>
      {process.env.PUBLIC_DEMO_MODE === "true" && <DemoReplayButton />}
      <div className="mt-8 flex items-baseline justify-between">
        <h2 className="text-lg font-semibold">Recent alerts</h2>
        <Link href="/app/alerts" className="text-xs text-accent">View all</Link>
      </div>
      <div className="mt-3 divide-y divide-ink-800 rounded-lg border border-ink-800">
        {recent.length === 0 ? (
          <div className="p-6">
            <p className="text-sm text-ink-300">No onsale signals yet.</p>
            <Link href="/app/events" className="mt-2 inline-block text-sm text-accent">
              Browse events to watch
            </Link>
          </div>
        ) : (
          recent.map((alert) => (
            <div key={alert.drop_id} className="flex items-center gap-4 p-4">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{alert.title}</p>
                <p className="mt-1 text-xs text-ink-400">
                  {alert.change.replaceAll("_", " ")} ·{" "}
                  {new Date(alert.detected_at).toLocaleString()}
                </p>
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
