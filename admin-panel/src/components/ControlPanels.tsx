"use client";

import { useCallback, useEffect, useState } from "react";
import { relativeTime, score, shortDate } from "@/lib/format";
import type { LinkRow, PreferenceRow, WatchRow } from "@/lib/types";
import { Button, Empty, Panel, Tag } from "./ui";

function useResource<T>(url: string, key: string) {
  const [rows, setRows] = useState<T[] | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const body = (await res.json()) as Record<string, T[]>;
      setRows(body[key] ?? []);
      setError("");
    } catch (cause) {
      setRows([]);
      setError(cause instanceof Error ? cause.message : "Request failed");
    }
  }, [key, url]);

  useEffect(() => {
    // Initial fetch synchronizes this client component with its API resource.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);

  return { rows, reload, error };
}

function useMutation(reload: () => Promise<void>) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (url: string, init: RequestInit) => {
    setBusy(true);
    try {
      const res = await fetch(url, init);
      if (!res.ok) throw new Error(`Update failed (${res.status})`);
      setError("");
      await reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Update failed");
    } finally {
      setBusy(false);
    }
  };

  return { run, busy, error };
}

function ErrorMessage({ children }: { children: string }) {
  return children ? <p className="px-4 py-2 text-xs text-danger">{children}</p> : null;
}

export function WatchlistPanel() {
  const { rows, reload, error: loadError } = useResource<WatchRow>("/api/watchlist", "watchlist");
  const mutation = useMutation(reload);

  const toggle = async (row: WatchRow) => {
    await mutation.run("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: row.event_id, active: !row.active }),
    });
  };

  return (
    <Panel
      title="Watchlist"
      subtitle="Events on the 2s availability loop"
      action={<Button onClick={reload}>refresh</Button>}
    >
      <ErrorMessage>{loadError || mutation.error}</ErrorMessage>
      {rows === null ? (
        <Empty>loading…</Empty>
      ) : rows.length === 0 ? (
        <Empty>
          Empty. The harness promotes events here when an announcement clears the hype threshold.
        </Empty>
      ) : (
        <ul className="divide-y divide-ink-800/70">
          {rows.map((row) => (
            <li key={row.event_id} className="flex items-start gap-3 px-4 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span
                    className={`truncate text-sm ${row.active ? "text-ink-100" : "text-ink-400 line-through"}`}
                  >
                    {row.title}
                  </span>
                  <span className="shrink-0 text-[11px] text-ink-400">
                    {shortDate(row.starts_at)}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-400">
                  <span>{row.tier}</span>
                  <span>·</span>
                  <span>via {row.watch_source}</span>
                  <span>·</span>
                  <span>polled {relativeTime(row.last_polled_at)}</span>
                  {!row.pollable && (
                    <Tag
                      tone="danger"
                      title={`Only the 'ra' adapter polls availability. This row names '${row.watch_source}'.${
                        row.resolved_to
                          ? ` Matched to "${row.resolved_to}" — the harness repoints this onto the RA row within ~30s.`
                          : " No RA counterpart has been resolved yet."
                      }`}
                    >
                      {row.resolved_to ? "inert · match found" : "inert"}
                    </Tag>
                  )}
                </div>
              </div>
              <Button
                onClick={() => toggle(row)}
                tone={row.active ? "danger" : "good"}
                disabled={mutation.busy}
              >
                {row.active ? "pause" : "resume"}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

export function PreferencesPanel() {
  const { rows, reload, error: loadError } = useResource<PreferenceRow>(
    "/api/preferences",
    "preferences",
  );
  const mutation = useMutation(reload);

  const toggle = async (row: PreferenceRow) => {
    await mutation.run("/api/preferences", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: row.id, active: !row.active }),
    });
  };

  const remove = async (row: PreferenceRow) => {
    if (!window.confirm(`Delete preference "${row.label}"?`)) return;
    await mutation.run(`/api/preferences?id=${encodeURIComponent(row.id)}`, {
      method: "DELETE",
    });
  };

  return (
    <Panel
      title="Preferences"
      subtitle="What hype scoring matches against"
      action={<Button onClick={reload}>refresh</Button>}
    >
      <ErrorMessage>{loadError || mutation.error}</ErrorMessage>
      {rows === null ? (
        <Empty>loading…</Empty>
      ) : rows.length === 0 ? (
        <Empty>
          None set, so every drop scores zero and nothing will be reserved. Add one with{" "}
          <code className="text-ink-300">python -m harness prefs add --kind artist --label &quot;…&quot;</code>
        </Empty>
      ) : (
        <ul className="divide-y divide-ink-800/70">
          {rows.map((row) => (
            <li key={row.id} className="flex items-center gap-3 px-4 py-2">
              <span className="w-14 shrink-0 text-[10px] uppercase tracking-wider text-ink-400">
                {row.kind}
              </span>
              <span
                className={`min-w-0 flex-1 truncate text-sm ${row.active ? "text-ink-100" : "text-ink-400 line-through"}`}
              >
                {row.label}
              </span>
              {!row.has_embedding && (
                <Tag tone="danger" title="No vector stored, so this preference can never match.">
                  no vector
                </Tag>
              )}
              <span className="shrink-0 font-mono text-[11px] text-ink-400">
                ×{row.weight.toFixed(2)}
              </span>
              <Button onClick={() => toggle(row)} disabled={mutation.busy}>
                {row.active ? "mute" : "unmute"}
              </Button>
              <Button onClick={() => remove(row)} tone="danger" disabled={mutation.busy}>
                del
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

export function LinksPanel() {
  const { rows, reload, error: loadError } = useResource<LinkRow>("/api/links", "links");
  const mutation = useMutation(reload);

  const decide = async (row: LinkRow, confirm: boolean) => {
    await mutation.run("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: row.event_id, confirm }),
    });
  };

  return (
    <Panel
      title="Cross-source links"
      subtitle="Machine matches awaiting confirmation"
      action={<Button onClick={reload}>refresh</Button>}
    >
      <ErrorMessage>{loadError || mutation.error}</ErrorMessage>
      {rows === null ? (
        <Empty>loading…</Empty>
      ) : rows.length === 0 ? (
        <Empty>Nothing to review. The resolver refuses ambiguous matches rather than guessing.</Empty>
      ) : (
        <ul className="divide-y divide-ink-800/70">
          {rows.map((row) => (
            <li key={row.event_id} className="px-4 py-2.5">
              <div className="flex items-center gap-2 text-xs">
                <span className="shrink-0 rounded bg-ink-800 px-1.5 py-0.5 text-[10px] text-ink-300">
                  {row.from_source}
                </span>
                <span className="min-w-0 flex-1 truncate text-ink-100">{row.from_title}</span>
                <span className="shrink-0 font-mono text-ink-400">
                  {score(row.match_confidence)}
                </span>
              </div>
              <div className="mt-1 flex items-center gap-2 text-xs">
                <span className="shrink-0 rounded bg-ink-800 px-1.5 py-0.5 text-[10px] text-ink-300">
                  {row.to_source}
                </span>
                <span className="min-w-0 flex-1 truncate text-ink-400">{row.to_title}</span>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <span className="text-[11px] text-ink-400">
                  {shortDate(row.from_starts_at)} → {shortDate(row.to_starts_at)}
                </span>
                <span className="ml-auto flex gap-2">
                  <Button onClick={() => decide(row, true)} tone="good" disabled={mutation.busy}>
                    same show
                  </Button>
                  <Button
                    onClick={() => decide(row, false)}
                    tone="danger"
                    disabled={mutation.busy}
                    title="Deletes the link so the resolver can reconsider this event."
                  >
                    reject
                  </Button>
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
