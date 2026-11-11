import Link from "next/link";
import { auth } from "@/auth";
import { query } from "@/lib/db";
import { DemoReplayButton } from "@/components/DemoReplayButton";
import { ArrowUpRight } from "@/components/ui";

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
    <section className="pb-16 pt-10 sm:pt-16">
      <div className="grid items-end gap-8 lg:grid-cols-[1fr_auto]">
        <div>
          <p className="eyebrow">Your signal desk</p>
          <h1 className="page-title mt-5 max-w-3xl">Your nights, in one place.</h1>
          <p className="mt-4 max-w-xl text-base leading-7 text-ink-400">
            Detroit&apos;s live catalogue is being matched against your follows and discovery
            profile.
          </p>
        </div>
        <Link href="/app/discover" className="button-primary gap-2">
          Explore tonight
          <ArrowUpRight />
        </Link>
      </div>

      <div className="mt-14 flex flex-wrap gap-x-12 gap-y-6 border-y border-white/[0.08] py-7">
        {[
          ["Active watches", stats?.watches ?? 0, "/app/events"],
          ["Signals recorded", stats?.alerts ?? 0, "/app/alerts"],
          ["Emails delivered", stats?.sent ?? 0, "/app/alerts"],
        ].map(([label, value, href]) => (
          <Link
            key={label}
            href={String(href)}
            className="group min-w-36 rounded-lg py-1 focus-visible:outline-offset-4"
          >
            <p className="text-3xl font-medium tracking-[-0.04em]">{value}</p>
            <p className="mt-1 text-xs text-ink-400 transition-colors group-hover:text-ink-100">
              {label} <span aria-hidden="true">↗</span>
            </p>
          </Link>
        ))}
        <div className="ml-auto hidden items-center gap-2 text-xs text-ink-400 sm:flex">
          <span className="h-1.5 w-1.5 rounded-full bg-ink-600" />
          First observation stays silent
        </div>
      </div>

      {process.env.PUBLIC_DEMO_MODE === "true" && <DemoReplayButton />}

      <div className="mt-12 flex items-baseline justify-between">
        <div>
          <p className="eyebrow">History</p>
          <h2 className="mt-2 text-2xl font-medium tracking-[-0.03em]">Recent onsale signals</h2>
        </div>
        <Link href="/app/alerts" className="text-sm text-ink-300 hover:text-ink-100">
          View all
        </Link>
      </div>
      <div className="surface mt-5 divide-y divide-white/[0.07] overflow-hidden rounded-2xl">
        {recent.length === 0 ? (
          <div className="flex flex-col items-start gap-4 p-8 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-base font-medium">No onsale signals yet</p>
              <p className="mt-1 text-sm text-ink-400">
                First observations establish quiet baselines, so you never get a false cold-start
                alert.
              </p>
            </div>
            <Link href="/app/events" className="button-secondary shrink-0">
              Browse events
            </Link>
          </div>
        ) : (
          recent.map((alert) => (
            <Link
              key={alert.drop_id}
              href="/app/alerts"
              className="flex items-center gap-5 p-5 transition-colors hover:bg-white/[0.035] sm:px-6"
            >
              <span className="signal-dot h-2 w-2 shrink-0 rounded-full bg-signal" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{alert.title}</p>
                <p className="mt-1 text-xs text-ink-400">
                  {alert.change.replaceAll("_", " ")} ·{" "}
                  {new Date(alert.detected_at).toLocaleString()}
                </p>
              </div>
              <span className="hidden rounded-full border border-white/10 px-2.5 py-1 text-[10px] uppercase tracking-wider text-ink-400 sm:inline-flex">
                audited
              </span>
            </Link>
          ))
        )}
      </div>
    </section>
  );
}
