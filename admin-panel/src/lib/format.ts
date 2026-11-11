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

export const STATE_TONE: Record<string, string> = {
  "IN FLIGHT": "bg-accent/15 text-accent ring-accent/40",
  NOTIFIED: "bg-signal/15 text-signal ring-signal/40",
  SKIPPED: "bg-ink-800 text-ink-400 ring-ink-700",
  FAILED: "bg-danger/15 text-danger ring-danger/40",
};

export const CHANGE_LABEL: Record<string, string> = {
  new_ticketed: "tickets live",
  tier_available: "tier / presale",
  returned_to_sale: "returns",
};
