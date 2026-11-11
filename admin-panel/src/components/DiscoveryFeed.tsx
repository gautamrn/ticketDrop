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
  };

  return (
    <section className="py-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-widest text-accent">For you</p>
          <h1 className="mt-1 text-3xl font-semibold">Discover upcoming events</h1>
          <p className="mt-2 max-w-2xl text-sm text-ink-400">
            Deterministic matches from your metro, genres, and optional budget. Discovery never
            sends email until you choose Watch.
          </p>
        </div>
        <Link href="/app/settings" className="rounded-md border border-ink-700 px-3 py-2 text-sm">
          Edit preferences
        </Link>
      </div>

      {profile?.enabled && (
        <div className="mt-5 flex flex-wrap gap-2 text-xs text-ink-300">
          {profile.areaName && <span className="rounded-full bg-ink-800 px-3 py-1">{profile.areaName}</span>}
          {profile.genres.map((genre) => (
            <span key={genre} className="rounded-full bg-ink-800 px-3 py-1">{genre}</span>
          ))}
          {profile.maxPrice !== null && (
            <span className="rounded-full bg-ink-800 px-3 py-1">
              ≤ {profile.maxPrice} {profile.currency}
            </span>
          )}
        </div>
      )}

      {error && <p role="alert" className="mt-5 text-sm text-danger">{error}</p>}
      <div className="mt-6 grid gap-4 md:grid-cols-2">
        {loading ? (
          <p className="text-sm text-ink-400">Matching the live catalogue…</p>
        ) : !profile?.enabled || profile.genres.length === 0 ? (
          <div className="rounded-lg border border-ink-800 p-6">
            <p className="text-sm text-ink-300">Discovery is not configured yet.</p>
            <Link href="/app/settings" className="mt-2 inline-block text-sm text-accent">
              Choose genres and enable discovery
            </Link>
          </div>
        ) : events.length === 0 ? (
          <div className="rounded-lg border border-ink-800 p-6">
            <p className="text-sm text-ink-300">No current events satisfy every filter.</p>
            <p className="mt-2 text-xs text-ink-400">
              Try removing the maximum price or adding another exact RA genre.
            </p>
          </div>
        ) : (
          events.map((row) => (
            <article key={row.id} className="flex flex-col rounded-lg border border-ink-800 bg-ink-900 p-5">
              <div className="flex flex-wrap gap-2">
                {row.match_reasons.map((reason) => (
                  <span key={reason} className="rounded-full bg-ink-800 px-2.5 py-1 text-[11px] text-signal">
                    {reason}
                  </span>
                ))}
              </div>
              <h2 className="mt-4 text-lg font-medium">{row.title}</h2>
              <p className="mt-1 text-xs text-ink-400">
                {row.starts_at ? new Date(row.starts_at).toLocaleString() : "Date unavailable"}
                {row.venue ? ` · ${row.venue}` : ""}
              </p>
              {row.artists.length > 0 && (
                <p className="mt-2 text-sm text-ink-300">{row.artists.join(", ")}</p>
              )}
              <div className="mt-auto flex flex-wrap items-center gap-3 pt-5 text-xs">
                {row.cost && <span className="text-ink-300">{row.cost}</span>}
                {row.content_url && (
                  <a href={row.content_url} target="_blank" rel="noreferrer" className="text-accent">
                    RA listing ↗
                  </a>
                )}
                <button
                  onClick={() => void toggleWatch(row)}
                  className={`ml-auto rounded-md px-3 py-1.5 text-sm ${
                    row.watched ? "border border-ink-600 text-ink-300" : "bg-accent text-white"
                  }`}
                >
                  {row.watched ? "Unwatch" : "Watch for drops"}
                </button>
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
