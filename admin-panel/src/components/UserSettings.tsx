"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { signOut } from "next-auth/react";

type Props = {
  initial: {
    ra_area_id: number | null;
    ra_area_name: string | null;
    discovery_enabled: boolean;
    discovery_genres: string[];
    discovery_max_price: number | null;
    discovery_currency: string;
  } | null;
  email: string;
  preferences: Array<{ id: string; label: string; kind: string; active: boolean }>;
  areas: Array<{ id: number; name: string }>;
};

export function UserSettings({ initial, email, preferences: initialPreferences, areas }: Props) {
  const [areaId, setAreaId] = useState(initial?.ra_area_id ?? areas[0]?.id ?? 0);
  const [preferences, setPreferences] = useState(initialPreferences);
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState("artist");
  const [message, setMessage] = useState("");
  const [discoveryEnabled, setDiscoveryEnabled] = useState(
    initial?.discovery_enabled ?? false,
  );
  const [discoveryGenres, setDiscoveryGenres] = useState(
    initial?.discovery_genres.join(", ") ?? "",
  );
  const [discoveryMaxPrice, setDiscoveryMaxPrice] = useState(
    initial?.discovery_max_price?.toString() ?? "",
  );
  const [discoveryCurrency, setDiscoveryCurrency] = useState(
    initial?.discovery_currency ?? "USD",
  );

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const maxPrice = discoveryMaxPrice.trim() === "" ? null : Number(discoveryMaxPrice);
    const response = await fetch("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        areaId,
        alertEmail: email,
        discoveryEnabled,
        discoveryGenres: discoveryGenres.split(","),
        discoveryMaxPrice: maxPrice,
        discoveryCurrency,
      }),
    });
    if (response.ok) {
      setMessage("Settings saved. Your discovery feed has been refreshed.");
    } else {
      const body = (await response.json()) as { error?: string };
      setMessage(body.error ?? "Could not save settings.");
    }
  };

  const addPreference = async (event: FormEvent) => {
    event.preventDefault();
    if (!label.trim()) return;
    const response = await fetch("/api/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, kind }),
    });
    if (response.ok) {
      const preference = (await response.json()) as Props["preferences"][number];
      setPreferences((current) => [
        ...current.filter(
          (item) =>
            !(item.kind === preference.kind && item.label.toLowerCase() === preference.label.toLowerCase()),
        ),
        preference,
      ]);
      setLabel("");
      setMessage("Preference saved. Matching shows can now be watched automatically.");
    } else {
      setMessage("Could not add preference.");
    }
  };

  const removePreference = async (id: string) => {
    const response = await fetch(`/api/preferences?id=${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    if (response.ok) {
      setPreferences((current) => current.filter((item) => item.id !== id));
      setMessage("Preference removed.");
    } else {
      setMessage("Could not remove preference.");
    }
  };

  const deleteAccount = async () => {
    if (!window.confirm("Delete your TicketDrop account and all subscriptions?")) return;
    const response = await fetch("/api/account", { method: "DELETE" });
    if (response.ok) await signOut({ callbackUrl: "/" });
    else setMessage("Could not delete account.");
  };

  return (
    <section className="grid gap-8 py-8 lg:grid-cols-2">
      <div>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="mt-1 text-sm text-ink-400">Choose one metro enumerated by this deployment.</p>
        <form onSubmit={save} className="mt-6 space-y-5 rounded-lg border border-ink-800 p-5">
          <label className="block text-sm">
            <span className="text-ink-300">Metro</span>
            <select
              value={areaId}
              onChange={(event) => setAreaId(Number(event.target.value))}
              className="mt-2 w-full rounded-md border border-ink-700 bg-ink-900 px-3 py-2"
            >
              {areas.map((area) => (
                <option key={area.id} value={area.id}>{area.name}</option>
              ))}
            </select>
            {areas.length === 0 && (
              <span className="mt-2 block text-xs text-danger">
                No metros are configured. Set RA_AREAS on the deployment.
              </span>
            )}
          </label>
          <div className="block text-sm">
            <span className="text-ink-300">Alert email</span>
            <p className="mt-2 rounded-md border border-ink-700 bg-ink-950 px-3 py-2 text-ink-400">
              {email}
            </p>
            <span className="mt-1 block text-xs text-ink-400">Uses your verified sign-in address.</span>
          </div>
          <fieldset className="space-y-4 border-t border-ink-800 pt-5">
            <legend className="text-sm font-medium">Discovery feed</legend>
            <label className="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                checked={discoveryEnabled}
                onChange={(event) => setDiscoveryEnabled(event.target.checked)}
                className="mt-1"
              />
              <span>
                <span className="block text-ink-300">Show preference-based discoveries</span>
                <span className="mt-1 block text-xs text-ink-400">
                  Discoveries stay in the app. Only explicit follows and exact-name matches email
                  you.
                </span>
              </span>
            </label>
            <label className="block text-sm">
              <span className="text-ink-300">Genres</span>
              <input
                value={discoveryGenres}
                onChange={(event) => setDiscoveryGenres(event.target.value)}
                placeholder="techno, house"
                disabled={!discoveryEnabled}
                className="mt-2 w-full rounded-md border border-ink-700 bg-ink-900 px-3 py-2 disabled:opacity-50"
              />
              <span className="mt-1 block text-xs text-ink-400">
                Comma-separated exact RA genre labels, up to eight.
              </span>
            </label>
            <div className="grid grid-cols-[1fr_auto] gap-2">
              <label className="block text-sm">
                <span className="text-ink-300">Maximum listed price</span>
                <input
                  type="number"
                  min="0"
                  max="10000"
                  step="0.01"
                  value={discoveryMaxPrice}
                  onChange={(event) => setDiscoveryMaxPrice(event.target.value)}
                  placeholder="No limit"
                  disabled={!discoveryEnabled}
                  className="mt-2 w-full rounded-md border border-ink-700 bg-ink-900 px-3 py-2 disabled:opacity-50"
                />
              </label>
              <label className="block text-sm">
                <span className="text-ink-300">Currency</span>
                <select
                  value={discoveryCurrency}
                  onChange={(event) => setDiscoveryCurrency(event.target.value)}
                  disabled={!discoveryEnabled}
                  className="mt-2 rounded-md border border-ink-700 bg-ink-900 px-3 py-2 disabled:opacity-50"
                >
                  <option value="USD">USD</option>
                  <option value="GBP">GBP</option>
                  <option value="EUR">EUR</option>
                </select>
              </label>
            </div>
            <p className="text-xs text-ink-400">
              Events with unknown or differently denominated prices are excluded when a maximum is
              set.
            </p>
          </fieldset>
          <button
            disabled={areas.length === 0}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            Save settings
          </button>
          {discoveryEnabled && (
            <Link href="/app/discover" className="ml-3 text-sm text-accent">
              Open discovery feed
            </Link>
          )}
        </form>
      </div>
      <div>
        <h2 className="text-lg font-semibold">Followed names</h2>
        <p className="mt-1 text-sm text-ink-400">
          Exact artist or venue names automatically create shared event watches.
        </p>
        <form onSubmit={addPreference} className="mt-6 flex gap-2">
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            className="rounded-md border border-ink-700 bg-ink-900 px-3 py-2 text-sm"
          >
            <option value="artist">Artist</option>
            <option value="venue">Venue</option>
          </select>
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="e.g. Prospa"
            className="min-w-0 flex-1 rounded-md border border-ink-700 bg-ink-900 px-3 py-2 text-sm"
          />
          <button className="rounded-md border border-accent px-3 py-2 text-sm text-accent">Add</button>
        </form>
        <ul className="mt-4 divide-y divide-ink-800 rounded-lg border border-ink-800">
          {preferences.length === 0 ? (
            <li className="p-4 text-sm text-ink-400">No preferences yet.</li>
          ) : (
            preferences.map((preference) => (
              <li key={preference.id} className="flex items-center gap-3 p-3 text-sm">
                <span className="w-16 text-xs uppercase text-ink-400">{preference.kind}</span>
                <span className="min-w-0 flex-1 truncate">{preference.label}</span>
                <button
                  type="button"
                  onClick={() => void removePreference(preference.id)}
                  className="text-xs text-danger"
                >
                  Remove
                </button>
              </li>
            ))
          )}
        </ul>
        <p role="status" className="mt-4 min-h-5 text-sm text-ink-300">{message}</p>
        <div className="mt-8 border-t border-ink-800 pt-5">
          <h2 className="text-sm font-semibold">Account data</h2>
          <div className="mt-3 flex gap-3">
            <a href="/api/account" className="rounded-md border border-ink-700 px-3 py-2 text-xs">
              Export JSON
            </a>
            <button
              type="button"
              onClick={() => void deleteAccount()}
              className="rounded-md border border-danger px-3 py-2 text-xs text-danger"
            >
              Delete account
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
