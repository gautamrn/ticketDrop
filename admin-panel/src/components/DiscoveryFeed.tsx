"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type DiscoveryProfile = {
  enabled: boolean;
  areaId: number | null;
  areaName: string | null;
  genres: string[];
  maxPrice: number | null;
  currency: string;
};

type DiscoveryEvent = {
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
  match_reasons: string[];
};

export function DiscoveryFeed() {
  const [profile, setProfile] = useState<DiscoveryProfile | null>(null);
  const [events, setEvents] = useState<DiscoveryEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const response = await fetch("/api/discover", { cache: "no-store" });
        if (!response.ok) throw new Error(`Could not load discoveries (${response.status})`);
        const body = (await response.json()) as {
          profile: DiscoveryProfile;
          events: DiscoveryEvent[];
        };
        setProfile(body.profile);
        setEvents(body.events);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Could not load discoveries");
      } finally {
        setLoading(false);
      }
    };
    void load();
  }, []);

  const toggleWatch = async (row: DiscoveryEvent) => {
    if (busyId) return;
    setBusyId(row.id);
    try {
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
        current.map((event) =>
          event.id === row.id
            ? {
                ...event,
                watched: !event.watched,
                subscription_origin: event.watched ? null : "explicit",
              }
            : event,
        ),
      );
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="pb-16 pt-10 sm:pt-16">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">For you</p>
          <h1 className="page-title mt-5">Find your next night.</h1>
          <p className="mt-5 max-w-2xl text-base leading-7 text-ink-400">
            Deterministic matches from your metro, genres, and optional budget. Discovery never
            sends email until you choose Watch.
          </p>
        </div>
        <Link href="/app/settings" className="button-secondary">
          Edit preferences
        </Link>
      </div>

      {profile?.enabled && (
        <div className="mt-10 flex flex-wrap items-center gap-2 border-y border-white/[0.07] py-5 text-xs text-ink-300">
          <span className="mr-2 text-ink-400">Matching</span>
          {profile.areaName && (
            <Link
              href="/app/settings"
              className="rounded-full border border-white/10 px-3 py-1.5 transition-colors hover:border-white/25 hover:text-ink-100"
            >
              {profile.areaName}
            </Link>
          )}
          {profile.genres.map((genre) => (
            <Link
              key={genre}
              href="/app/settings"
              className="rounded-full border border-white/10 px-3 py-1.5 transition-colors hover:border-white/25 hover:text-ink-100"
            >
              {genre}
            </Link>
          ))}
          {profile.maxPrice !== null && (
            <Link
              href="/app/settings"
              className="rounded-full border border-white/10 px-3 py-1.5 transition-colors hover:border-white/25 hover:text-ink-100"
            >
              ≤ {profile.maxPrice} {profile.currency}
            </Link>
          )}
          {!loading && events.length > 0 && (
            <span className="ml-auto text-ink-400">{events.length} current matches</span>
          )}
        </div>
      )}

      {error && <p role="alert" className="mt-5 text-sm text-danger">{error}</p>}
      <div className="mt-8 grid gap-4 md:grid-cols-2">
        {loading ? (
          <div className="surface col-span-full rounded-2xl p-8 text-sm text-ink-400">
            Matching the live catalogue…
          </div>
        ) : !profile?.enabled || profile.genres.length === 0 ? (
          <div className="surface rounded-2xl p-8">
            <p className="text-sm text-ink-300">Discovery is not configured yet.</p>
            <Link href="/app/settings" className="mt-2 inline-block text-sm text-accent">
              Choose genres and enable discovery
            </Link>
          </div>
        ) : events.length === 0 ? (
          <div className="surface rounded-2xl p-8">
            <p className="text-sm text-ink-300">No current events satisfy every filter.</p>
            <p className="mt-2 text-xs text-ink-400">
              Try removing the maximum price or adding another exact RA genre.
            </p>
          </div>
        ) : (
          events.map((row, index) => (
            <article
              key={row.id}
              className="surface group relative flex min-h-72 flex-col rounded-2xl p-6 transition-colors hover:border-white/[0.18] sm:p-7"
            >
              {row.content_url && (
                <a
                  href={row.content_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Open ${row.title} on Resident Advisor`}
                  className="absolute inset-0 rounded-2xl"
                />
              )}
              <div className="pointer-events-none relative z-10 flex flex-wrap gap-2">
                {row.match_reasons.map((reason) => (
                  <span key={reason} className="rounded-full border border-signal/15 bg-signal/[0.06] px-2.5 py-1 text-[11px] text-signal">
                    {reason}
                  </span>
                ))}
                <span className="ml-auto font-mono text-[10px] text-ink-600">
                  {String(index + 1).padStart(2, "0")}
                </span>
              </div>
              <h2 className="pointer-events-none relative z-10 mt-8 text-xl font-medium tracking-[-0.025em]">
                {row.title}
              </h2>
              <p className="pointer-events-none relative z-10 mt-2 text-xs leading-5 text-ink-400">
                {row.starts_at ? new Date(row.starts_at).toLocaleString() : "Date unavailable"}
                {row.venue ? ` · ${row.venue}` : ""}
              </p>
              {row.artists.length > 0 && (
                <p className="pointer-events-none relative z-10 mt-2 text-sm text-ink-300">
                  {row.artists.join(", ")}
                </p>
              )}
              <div className="relative z-10 mt-auto flex flex-wrap items-center gap-3 pt-5 text-xs">
                {row.cost && <span className="text-ink-300">{row.cost}</span>}
                {row.content_url && (
                  <a href={row.content_url} target="_blank" rel="noreferrer" className="text-accent">
                    RA listing ↗
                  </a>
                )}
                <button
                  onClick={() => void toggleWatch(row)}
                  aria-pressed={row.watched}
                  disabled={busyId !== null}
                  className={`ml-auto rounded-full px-4 py-2 text-sm font-medium transition-colors ${
                    row.watched
                      ? "border border-white/12 text-ink-300 hover:bg-white/[0.05]"
                      : "bg-accent text-ink-950 hover:bg-[#e3ff8e]"
                  } disabled:cursor-wait disabled:opacity-50`}
                >
                  {busyId === row.id ? "Updating…" : row.watched ? "Unwatch" : "Watch for drops"}
                </button>
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
