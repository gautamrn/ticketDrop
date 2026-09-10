"use client";

import { useEffect, useState } from "react";
import type { Health } from "@/lib/types";

export function HealthBar() {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) throw new Error(`Health request failed (${res.status})`);
        const body = (await res.json()) as Health;
        if (!cancelled) setHealth(body);
      } catch {
        if (!cancelled) setHealth((current) => (current ? { ...current, redis: false } : null));
      }
    };

    void poll();
    // Polled rather than pushed: these are aggregate counters, not events, and
    // a five second staleness on a queue depth changes no decision.
    const timer = setInterval(poll, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const counts = health?.counts ?? {};
  const streamLengths = Object.fromEntries(
    (health?.streams ?? []).map((stream) => [stream.name, stream.length]),
  );

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-ink-800 bg-ink-900/60 px-4 py-3">
      <Dot ok={health?.redis} label="redis" />
      <Dot ok={health?.postgres} label="postgres" />

      <Divider />

      <Stat label="in flight" value={counts.inFlight} tone={counts.inFlight ? "accent" : undefined} />
      <Stat label="notified" value={counts.notified} tone="signal" />
      <Stat label="skipped" value={counts.skipped} />
      <Stat label="failed" value={counts.failed} tone={counts.failed ? "danger" : undefined} />

      <Divider />

      <Stat
        label="group lag"
        value={health?.lag ?? undefined}
        tone={(health?.lag ?? 0) > 50 ? "danger" : undefined}
        title="Entries on ticket:drops the harness group has not read. Sustained growth means the harness cannot keep up."
      />
      <Stat
        label="pending"
        value={health?.pending ?? undefined}
        tone={(health?.pending ?? 0) > 20 ? "warn" : undefined}
        title="Delivered but unacknowledged. XAUTOCLAIM reaps these after 60s idle."
      />
      <Stat
        label="consumers"
        value={health?.consumers}
        tone={health && health.consumers === 0 ? "danger" : undefined}
        title="Harness workers attached to the consumer group. Zero means nothing is processing drops."
      />
      <Stat label="input stream" value={streamLengths["ticket:drops"]} />
      <Stat label="result stream" value={streamLengths["ticket:drops:results"]} />

      <Divider />

      <Stat label="watching" value={counts.watchlist} />
      <Stat
        label="links to review"
        value={counts.pendingLinks}
        tone={counts.pendingLinks ? "warn" : undefined}
      />

      {health && health.activePreferences === 0 && (
        <span
          className="ml-auto rounded bg-danger/15 px-2 py-1 text-[11px] font-medium text-danger ring-1 ring-inset ring-danger/40"
          title="Hype scoring fails closed. With no preferences every drop scores zero and nothing is ever reserved or promoted."
        >
          no preferences — nothing will ever reserve
        </span>
      )}
      {health && health.activePreferences > 0 && !health.soundScoringAvailable && (
        <span
          className="ml-auto rounded bg-ink-800 px-2 py-1 text-[11px] text-ink-400 ring-1 ring-inset ring-ink-700"
          title="No artist has a sound_signature, so the 0.25 sound weight is excluded from the denominator rather than charged against every event."
        >
          sound signal unavailable — weight excluded
        </span>
      )}
    </div>
  );
}

function Dot({ ok, label }: { ok?: boolean; label: string }) {
  const tone = ok === undefined ? "bg-ink-600" : ok ? "bg-signal" : "bg-danger";
  return (
    <span className="flex items-center gap-1.5 text-[11px] text-ink-300">
      <span className={`h-1.5 w-1.5 rounded-full ${tone}`} />
      {label}
    </span>
  );
}

function Divider() {
  return <span className="h-4 w-px bg-ink-800" />;
}

function Stat({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value?: number;
  tone?: "signal" | "danger" | "warn" | "accent";
  title?: string;
}) {
  const tones = {
    signal: "text-signal",
    danger: "text-danger",
    warn: "text-warn",
    accent: "text-accent",
  };
  return (
    <span className="flex items-baseline gap-1.5 text-[11px]" title={title}>
      <span className="text-ink-400">{label}</span>
      <span className={`font-mono font-medium ${tone ? tones[tone] : "text-ink-100"}`}>
        {value ?? "—"}
      </span>
    </span>
  );
}
