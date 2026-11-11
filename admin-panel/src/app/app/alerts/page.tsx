import { auth } from "@/auth";
import { query } from "@/lib/db";

export default async function AlertsPage() {
  const session = await auth();
  const alerts = await query<{
    drop_id: string;
    title: string;
    venue: string | null;
    change: string;
    detected_at: string;
    content_url: string | null;
    ticket_link: string | null;
    detail: Record<string, unknown> | null;
    delivery_status: string | null;
    sent_at: string | null;
    last_error: string | null;
  }>(
    `SELECT d.id::text AS drop_id, e.title, v.name AS venue,
            d.change::text AS change, d.detected_at,
            e.content_url, e.ticket_link, transition.detail,
            nd.status::text AS delivery_status, nd.sent_at, nd.last_error
       FROM notification_deliveries nd
       JOIN drops d ON d.id = nd.drop_id
       JOIN events e ON e.id = d.event_id
       LEFT JOIN venues v ON v.id = e.venue_id
       LEFT JOIN LATERAL (
         SELECT detail FROM drop_transitions
          WHERE drop_id = d.id ORDER BY at DESC LIMIT 1
       ) transition ON true
      WHERE nd.user_id = $1::uuid
      ORDER BY d.detected_at DESC LIMIT 100`,
    [session!.user.id],
  );

  return (
    <section className="py-8">
      <h1 className="text-2xl font-semibold">Alert history</h1>
      <p className="mt-1 text-sm text-ink-400">
        Classified onsale signals and email delivery outcomes.
      </p>
      <div className="mt-6 space-y-3" aria-live="polite">
        {alerts.length === 0 ? (
          <p className="rounded-lg border border-ink-800 p-6 text-sm text-ink-400">
            No alerts yet. The first source observation is a baseline and never creates a false signal.
          </p>
        ) : (
          alerts.map((alert) => (
            <article key={alert.drop_id} className="rounded-lg border border-ink-800 bg-ink-900 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="font-medium">{alert.title}</h2>
                  <p className="mt-1 text-xs text-ink-400">
                    {alert.change.replaceAll("_", " ")}
                    {alert.venue ? ` · ${alert.venue}` : ""}
                    {" · "}
                    {new Date(alert.detected_at).toLocaleString()}
                  </p>
                </div>
                <div className="text-right">
                  <p className="mt-1 text-xs text-ink-400">
                    email {alert.delivery_status ?? "not queued"}
                  </p>
                </div>
              </div>
              <details className="mt-4 border-t border-ink-800 pt-3">
                <summary className="cursor-pointer text-xs text-ink-300">
                  Engineering details
                </summary>
                <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
                  {Object.entries(alert.detail ?? {})
                    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
                    .slice(0, 6)
                    .map(([name, value]) => (
                    <div key={name}>
                      <dt className="text-ink-400">{name}</dt>
                      <dd className="font-mono">{String(value)}</dd>
                    </div>
                  ))}
                  <div>
                    <dt className="text-ink-400">signal id</dt>
                    <dd className="truncate font-mono">{alert.drop_id}</dd>
                  </div>
                </dl>
                {alert.last_error && <p className="mt-3 text-xs text-danger">{alert.last_error}</p>}
              </details>
              <div className="mt-4 flex gap-4 text-sm">
              {alert.content_url && (
                <a
                  href={alert.content_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-4 inline-block text-sm text-accent"
                >
                  RA listing ↗
                </a>
              )}
              {alert.ticket_link && (
                <a
                  href={alert.ticket_link}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm text-accent"
                >
                  Seller ↗
                </a>
              )}
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
