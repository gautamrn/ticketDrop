"use client";

import { useCallback, useEffect, useState } from "react";
import { relativeTime, shortDate } from "@/lib/format";
import type { WatchRow } from "@/lib/types";
import { Button, Empty, Panel } from "./ui";

export function OperatorWatchlist() {
  const [rows, setRows] = useState<WatchRow[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      const response = await fetch("/api/watchlist", { cache: "no-store" });
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      const body = (await response.json()) as { watchlist: WatchRow[] };
      setRows(body.watchlist);
      setError("");
    } catch (cause) {
      setRows([]);
      setError(cause instanceof Error ? cause.message : "Request failed");
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);

  const toggle = async (row: WatchRow) => {
    setBusy(true);
    const response = await fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: row.event_id, active: !row.active }),
    });
    setBusy(false);
    if (!response.ok) {
      setError(`Update failed (${response.status})`);
      return;
    }
    await reload();
  };

  return (
    <Panel
      title="Shared watchlist"
      subtitle="One RA poll per show"
      action={<Button onClick={reload}>refresh</Button>}
    >
      {error && <p className="px-4 py-2 text-xs text-danger">{error}</p>}
      {rows === null ? (
        <Empty>loading…</Empty>
      ) : rows.length === 0 ? (
        <Empty>Subscriptions have not projected any shared watches yet.</Empty>
      ) : (
        <ul className="divide-y divide-ink-800/70">
          {rows.map((row) => (
            <li key={row.event_id} className="flex items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className={`truncate text-sm ${row.active ? "" : "text-ink-400 line-through"}`}>
                  {row.title}
                </p>
                <p className="mt-1 text-[11px] text-ink-400">
                  {shortDate(row.starts_at)} · {row.tier} · polled {relativeTime(row.last_polled_at)}
                </p>
              </div>
              <Button
                onClick={() => void toggle(row)}
                tone={row.active ? "danger" : "good"}
                disabled={busy}
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
