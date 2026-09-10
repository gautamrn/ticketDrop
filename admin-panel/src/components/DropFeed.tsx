"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CHANGE_LABEL, isTrue, latency, latencyTone, relativeTime, score } from "@/lib/format";
import type { FeedItem, TransitionRow } from "@/lib/types";
import { Button, Empty, EventLink, Panel, StateBadge, Tag } from "./ui";

const MAX_ITEMS = 300;

type Connection = "connecting" | "live" | "down";

export function DropFeed() {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [selected, setSelected] = useState<FeedItem | null>(null);
  const [freshIds, setFreshIds] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const source = new EventSource("/api/stream");

    source.addEventListener("backfill", (event) => {
      const history = parseEventData<FeedItem[]>((event as MessageEvent).data);
      if (!history) return;
      // Backfill is not "fresh" — flashing forty rows on load is noise.
      setItems(history.slice().reverse());
      setConnection("live");
    });

    source.addEventListener("drop", (event) => {
      const item = parseEventData<FeedItem>((event as MessageEvent).data);
      if (!item?.entry_id) return;
      setFreshIds((current) => new Set(current).add(item.entry_id));
      setConnection("live");
      setItems((current) => [item, ...current].slice(0, MAX_ITEMS));
    });

    source.addEventListener("error", () => {
      // EventSource reconnects on its own with backoff. Reporting the gap is
      // this handler's only job; retrying here would fight the browser.
      setConnection("down");
    });

    source.onopen = () => setConnection("live");

    return () => source.close();
  }, []);

  const stats = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of items) {
      const key = item.state ?? "UNKNOWN";
      counts[key] = (counts[key] ?? 0) + 1;
    }
    return counts;
  }, [items]);

  return (
    <>
      <Panel
        title="Live drops"
        subtitle={`${items.length} in view · ${stats.NOTIFIED ?? 0} notified · ${stats.SKIPPED ?? 0} skipped · ${stats.FAILED ?? 0} failed`}
        action={<ConnectionDot connection={connection} />}
      >
        {items.length === 0 ? (
          <Empty>
            Nothing yet. The feed tails <code className="text-ink-300">ticket:drops:results</code>,
            which only fills once the harness finishes a drop.
          </Empty>
        ) : (
          <ul className="divide-y divide-ink-800/70">
            {items.map((item) => (
              <FeedRow
                key={item.entry_id}
                item={item}
                fresh={freshIds.has(item.entry_id)}
                onSelect={() => setSelected(item)}
              />
            ))}
          </ul>
        )}
      </Panel>
      {selected && <DropDetail item={selected} onClose={() => setSelected(null)} />}
    </>
  );
}

function ConnectionDot({ connection }: { connection: Connection }) {
  const map = {
    connecting: { tone: "bg-warn", label: "connecting" },
    live: { tone: "bg-signal", label: "live" },
    down: { tone: "bg-danger", label: "reconnecting" },
  } as const;
  const { tone, label } = map[connection];
  return (
    <span className="flex shrink-0 items-center gap-1.5 text-[10px] uppercase tracking-wider text-ink-400">
      <span className={`h-1.5 w-1.5 rounded-full ${tone}`} />
      {label}
    </span>
  );
}

function FeedRow({
  item,
  fresh,
  onSelect,
}: {
  item: FeedItem;
  fresh: boolean;
  onSelect: () => void;
}) {
  const forced = isTrue(item.forced_alert);
  const queued = isTrue(item.queue_it);

  return (
    <li className={`px-4 py-2.5 hover:bg-ink-850/60 ${fresh ? "flash-in" : ""}`}>
      <div className="flex items-start gap-3">
        <StateBadge state={item.state} />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <button
              type="button"
              onClick={onSelect}
              className="truncate text-left text-sm font-medium text-ink-100 hover:text-accent"
            >
              {item.title || item.event_id || "untitled"}
            </button>
            <span className="shrink-0 text-[11px] text-ink-400">{relativeTime(item.at)}</span>
          </div>

          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-400">
            <span className="text-ink-300">{item.source}</span>
            <span>·</span>
            <span>{CHANGE_LABEL[item.change ?? ""] ?? item.change}</span>
            {item.venue && (
              <>
                <span>·</span>
                <span className="truncate">{item.venue}</span>
              </>
            )}
            <span>·</span>
            <span className="font-medium text-ink-300">hype {score(item.hype_score)}</span>

            {queued && (
              <Tag tone="warn" title="Queue-it waiting room: automation is refused by policy">
                queue-it
              </Tag>
            )}
            {forced && (
              <Tag tone="warn" title="Downgraded to alert-only because of the waiting room">
                forced alert
              </Tag>
            )}
            {item.healed_selector && <Tag tone="good">healed</Tag>}
            {item.error && (
              <Tag tone="danger" title={item.error}>
                error
              </Tag>
            )}
          </div>
        </div>

        {item.link && (
          <EventLink href={item.link} className="shrink-0 text-[11px] text-ink-400">
            open
          </EventLink>
        )}
      </div>
    </li>
  );
}

function DropDetail({ item, onClose }: { item: FeedItem; onClose: () => void }) {
  const [transitions, setTransitions] = useState<TransitionRow[] | null>(null);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    if (!item.drop_id) return;
    try {
      const res = await fetch(`/api/drops?drop_id=${encodeURIComponent(item.drop_id)}`);
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const body = (await res.json()) as { transitions: TransitionRow[] };
      setTransitions(body.transitions);
      setLoadError("");
    } catch (cause) {
      setTransitions([]);
      setLoadError(cause instanceof Error ? cause.message : "Request failed");
    }
  }, [item.drop_id]);

  useEffect(() => {
    // Opening the detail synchronizes it with the durable transition record.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const components = useMemo(() => {
    if (!item.hype_components) return null;
    try {
      return JSON.parse(item.hype_components) as Record<string, number | null>;
    } catch {
      return null;
    }
  }, [item.hype_components]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-ink-950/80 p-6 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="mt-10 max-h-[80vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-ink-700 bg-ink-900 shadow-2xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drop-detail-title"
      >
        <header className="flex items-start justify-between gap-4 border-b border-ink-800 px-5 py-4">
          <div className="min-w-0">
            <h3 id="drop-detail-title" className="truncate text-base font-semibold">
              {item.title || "untitled"}
            </h3>
            <p className="mt-1 font-mono text-[11px] text-ink-400">{item.drop_id}</p>
          </div>
          <Button onClick={onClose}>close</Button>
        </header>

        <div className="grid grid-cols-2 gap-x-6 gap-y-3 px-5 py-4 text-xs sm:grid-cols-3">
          <Field label="state" value={item.state} />
          <Field label="outcome" value={item.outcome} />
          <Field label="change" value={CHANGE_LABEL[item.change ?? ""] ?? item.change} />
          <Field label="hype" value={score(item.hype_score)} />
          <Field label="mode" value={item.mode} />
          <Field label="attempts" value={item.attempt_count} />
          <Field label="queue-it" value={isTrue(item.queue_it) ? "yes" : "no"} />
          <Field label="forced alert" value={isTrue(item.forced_alert) ? "yes" : "no"} />
          <Field label="source" value={item.source} />
        </div>

        {components && (
          <div className="border-t border-ink-800 px-5 py-4">
            <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-ink-400">
              Hype components
            </h4>
            <div className="space-y-1.5">
              {Object.entries(components).map(([name, value]) => (
                <div key={name} className="flex items-center gap-3 text-xs">
                  <span className="w-16 shrink-0 text-ink-400">{name}</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-800">
                    <div
                      className="h-full rounded-full bg-accent"
                      style={{ width: `${Math.round((value ?? 0) * 100)}%` }}
                    />
                  </div>
                  <span className="w-20 shrink-0 text-right font-mono text-ink-300">
                    {value === null ? "unavailable" : value.toFixed(3)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {item.error && (
          <div className="border-t border-ink-800 px-5 py-4">
            <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-danger">
              Error
            </h4>
            <p className="font-mono text-xs text-danger">{item.error}</p>
          </div>
        )}

        <div className="border-t border-ink-800 px-5 py-4">
          <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-ink-400">
            FSM transitions
          </h4>
          {loadError ? (
            <p className="text-xs text-danger">{loadError}</p>
          ) : transitions === null ? (
            <p className="text-xs text-ink-400">loading…</p>
          ) : transitions.length === 0 ? (
            <p className="text-xs text-ink-400">
              No rows. The drop may predate this database, or the stream entry outlived it.
            </p>
          ) : (
            <ol className="space-y-1.5">
              {transitions.map((t, index) => (
                <li key={index} className="flex items-center gap-3 text-xs">
                  <span className="w-24 shrink-0 text-ink-400">{t.from_state ?? "start"}</span>
                  <span className="text-ink-600">→</span>
                  <StateBadge state={t.to_state} />
                  <span className={`ml-auto font-mono ${latencyTone(t.latency_ms)}`}>
                    {latency(t.latency_ms)}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>

        {item.link && (
          <footer className="border-t border-ink-800 px-5 py-4">
            {/* Rendered verbatim: EDMTrain's terms require the event link to be
                displayed unmodified. */}
            <EventLink href={item.link} className="break-all font-mono text-[11px]">
              {item.link}
            </EventLink>
          </footer>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value?: string | null }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-ink-400">{label}</dt>
      <dd className="mt-0.5 truncate text-ink-100">{value || "—"}</dd>
    </div>
  );
}

function parseEventData<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}
