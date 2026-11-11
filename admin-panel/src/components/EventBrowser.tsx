"use client";

import { FormEvent, useEffect, useState } from "react";

type EventRow = {
  id: string;
  title: string;
  content_url: string | null;
  ticket_link: string | null;
  starts_at: string | null;
  cost: string | null;
  attending: number | null;
  venue: string | null;
  area_name: string | null;
  artists: string[];
  watched: boolean;
  subscription_origin: "explicit" | "scout" | null;
};

export function EventBrowser({ initialArea }: { initialArea?: number | null }) {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [query, setQuery] = useState("");
  const [area, setArea] = useState(initialArea ? String(initialArea) : "");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (query.trim()) params.set("q", query.trim());
    if (area) params.set("area", area);
    try {
      const response = await fetch(`/api/events?${params}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Could not load events (${response.status})`);
      const body = (await response.json()) as { events: EventRow[] };
      setEvents(body.events);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load events");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    // Initial catalogue load; searches are submitted explicitly to avoid a
    // request per keystroke against the shared database.
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void load();
  };

  const toggle = async (row: EventRow) => {
    const response = await fetch(
      row.watched ? `/api/subscriptions?eventId=${encodeURIComponent(row.id)}` : "/api/subscriptions",
      {
        method: row.watched ? "DELETE" : "POST",
        headers: { "Content-Type": "application/json" },
        body: row.watched ? undefined : JSON.stringify({ eventId: row.id }),
      },
    );
    if (!response.ok) {
      const body = (await response.json()) as { error?: string };
      setError(body.error ?? "Could not update watch");
      return;
    }
    setEvents((current) =>
      current.map((item) =>
        item.id === row.id
          ? {
              ...item,
              watched: !item.watched,
              subscription_origin: item.watched ? null : "explicit",
            }
          : item,
      ),
    );
  };

  return (
    <section className="py-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Upcoming RA events</h1>
          <p className="mt-1 text-sm text-ink-400">
            Watch up to 25 shows. Artist and venue matches are labeled scout; every subscriber
            shares one source poll per show.
          </p>
        </div>
        <form onSubmit={submit} className="flex flex-wrap gap-2">
          <label className="sr-only" htmlFor="event-search">
            Search title, artist, or venue
          </label>
          <input
            id="event-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Artist, event, or venue"
            className="rounded-md border border-ink-700 bg-ink-900 px-3 py-2 text-sm"
          />
          <input
            aria-label="RA area id"
            value={area}
            onChange={(event) => setArea(event.target.value.replace(/\D/g, ""))}
            placeholder="Area id"
            className="w-24 rounded-md border border-ink-700 bg-ink-900 px-3 py-2 text-sm"
          />
          <button className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white">
            Search
          </button>
        </form>
      </div>
      {error && <p role="alert" className="mt-4 text-sm text-danger">{error}</p>}
      <div className="mt-6 divide-y divide-ink-800 rounded-lg border border-ink-800">
        {loading ? (
          <p className="p-6 text-sm text-ink-400">Loading catalogue…</p>
        ) : events.length === 0 ? (
          <p className="p-6 text-sm text-ink-400">No matching RA events are in the catalogue yet.</p>
        ) : (
          events.map((row) => (
            <article key={row.id} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center">
              <div className="min-w-0 flex-1">
                <h2 className="truncate font-medium">{row.title}</h2>
                <p className="mt-1 truncate text-xs text-ink-400">
                  {row.starts_at ? new Date(row.starts_at).toLocaleString() : "Date unavailable"}
                  {row.venue ? ` · ${row.venue}` : ""}
                  {row.area_name ? ` · ${row.area_name}` : ""}
                </p>
                {row.artists.length > 0 && (
                  <p className="mt-1 truncate text-xs text-ink-300">{row.artists.join(", ")}</p>
                )}
                <div className="mt-2 flex gap-3 text-xs">
                  {row.content_url && (
                    <a href={row.content_url} target="_blank" rel="noreferrer" className="text-accent">
                      RA listing ↗
                    </a>
                  )}
                  {row.ticket_link && (
                    <a href={row.ticket_link} target="_blank" rel="noreferrer" className="text-accent">
                      Seller ↗
                    </a>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-3">
                {row.cost && <span className="text-xs text-ink-400">{row.cost}</span>}
                {row.watched && row.subscription_origin && (
                  <span className="rounded bg-ink-800 px-2 py-1 text-[10px] uppercase text-ink-300">
                    {row.subscription_origin}
                  </span>
                )}
                <button
                  onClick={() => void toggle(row)}
                  className={`rounded-md px-3 py-1.5 text-sm ${
                    row.watched ? "border border-ink-600 text-ink-300" : "bg-accent text-white"
                  }`}
                >
                  {row.watched ? "Unwatch" : "Watch"}
                </button>
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
