export function relativeTime(value?: string | null): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";

  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 0) return "now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

export function shortDate(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function latency(ms?: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function score(value?: string | number | null): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number.parseFloat(value);
  return Number.isNaN(parsed) ? "—" : parsed.toFixed(3);
}

export function isTrue(value?: string | boolean | null): boolean {
  return value === true || value === "true";
}

/**
 * Latency shown against the budget it is supposed to meet, not in isolation.
 * ARCHITECTURE.md §3: ingestion 50ms, evaluation 350ms, reservation 2500ms.
 */
export function latencyTone(ms?: number | null): string {
  if (ms === null || ms === undefined) return "text-ink-400";
  if (ms <= 350) return "text-signal";
  if (ms <= 2500) return "text-warn";
  return "text-danger";
}

export const STATE_TONE: Record<string, string> = {
  INGESTED: "bg-ink-700 text-ink-100 ring-ink-600",
  EVALUATING: "bg-accent/15 text-accent ring-accent/40",
  RESERVING: "bg-accent/20 text-accent ring-accent/50",
  SELF_HEAL: "bg-warn/15 text-warn ring-warn/40",
  NOTIFIED: "bg-signal/15 text-signal ring-signal/40",
  SKIPPED: "bg-ink-800 text-ink-400 ring-ink-700",
  FAILED: "bg-danger/15 text-danger ring-danger/40",
};

export const CHANGE_LABEL: Record<string, string> = {
  announced: "announced",
  new_ticketed: "tickets live",
  tier_available: "tier / presale",
  returned_to_sale: "returns",
};
