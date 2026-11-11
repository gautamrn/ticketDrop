"use client";

import { useCallback, useEffect, useState } from "react";
import { CHANGE_LABEL, relativeTime } from "@/lib/format";
import type { OperatorDrop } from "@/lib/types";
import { Button, Empty, EventLink, Panel, StateBadge, Tag } from "./ui";

export function OperatorDrops() {
  const [drops, setDrops] = useState<OperatorDrop[] | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      const response = await fetch("/api/drops", { cache: "no-store" });
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      const body = (await response.json()) as { drops: OperatorDrop[] };
      setDrops(body.drops);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
      setDrops([]);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void reload(), 0);
    const timer = window.setInterval(() => void reload(), 5000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [reload]);

  return (
    <Panel
      title="Recent onsale signals"
      subtitle="Durable rows from Postgres · refreshes every 5 seconds"
      action={<Button onClick={reload}>refresh</Button>}
    >
      {error && <p className="px-4 py-2 text-xs text-danger">{error}</p>}
      {drops === null ? (
        <Empty>loading…</Empty>
      ) : drops.length === 0 ? (
        <Empty>No classified signals have been recorded.</Empty>
      ) : (
        <ul className="divide-y divide-ink-800/70">
          {drops.map((drop) => (
            <li key={drop.drop_id} className="px-4 py-3">
              <div className="flex items-start gap-3">
                <StateBadge state={drop.state ?? "IN FLIGHT"} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{drop.title}</p>
                  <p className="mt-1 text-xs text-ink-400">
                    {CHANGE_LABEL[drop.change] ?? drop.change}
                    {drop.venue ? ` · ${drop.venue}` : ""}
                    {` · ${relativeTime(drop.detected_at)}`}
                  </p>
                  <Detail detail={drop.detail} />
                  {drop.error && <p className="mt-2 text-xs text-danger">{drop.error}</p>}
                </div>
                <div className="flex shrink-0 gap-3 text-xs">
                  <EventLink href={drop.content_url}>RA</EventLink>
                  {drop.ticket_link && <EventLink href={drop.ticket_link}>seller</EventLink>}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Detail({ detail }: { detail: Record<string, unknown> | null }) {
  if (!detail) return null;
  const fields = Object.entries(detail)
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
    .slice(0, 4);
  if (fields.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {fields.map(([name, value]) => (
        <Tag key={name}>{name.replaceAll("_", " ")}: {String(value)}</Tag>
      ))}
    </div>
  );
}
