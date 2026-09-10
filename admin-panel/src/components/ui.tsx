import { STATE_TONE } from "@/lib/format";

export function Panel({
  title,
  subtitle,
  action,
  children,
  className = "",
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`flex min-h-0 flex-col rounded-xl border border-ink-800 bg-ink-900/60 ${className}`}
    >
      <header className="flex items-baseline justify-between gap-3 border-b border-ink-800 px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold tracking-wide text-ink-100">{title}</h2>
          {subtitle && <p className="mt-0.5 truncate text-xs text-ink-400">{subtitle}</p>}
        </div>
        {action}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </section>
  );
}

export function StateBadge({ state }: { state?: string }) {
  const tone = STATE_TONE[state ?? ""] ?? "bg-ink-800 text-ink-300 ring-ink-700";
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wider ring-1 ring-inset ${tone}`}
    >
      {state ?? "—"}
    </span>
  );
}

export function Tag({
  children,
  tone = "neutral",
  title,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "warn" | "danger" | "good";
  title?: string;
}) {
  const tones = {
    neutral: "bg-ink-800 text-ink-300 ring-ink-700",
    warn: "bg-warn/15 text-warn ring-warn/40",
    danger: "bg-danger/15 text-danger ring-danger/40",
    good: "bg-signal/15 text-signal ring-signal/40",
  } as const;
  return (
    <span
      title={title}
      className={`inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-medium ring-1 ring-inset ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * An outbound link to a ticketing source.
 *
 * The href is rendered exactly as stored. EDMTrain's API terms require their
 * event link to be displayed unmodified wherever the event appears
 * (ARCHITECTURE.md §10.1), so this component must never append a tracking
 * parameter, shorten the URL, or route it through a redirect. `target` and `rel`
 * are attributes of the anchor and do not alter the URL itself.
 */
export function EventLink({
  href,
  children,
  className = "",
}: {
  href?: string | null;
  children: React.ReactNode;
  className?: string;
}) {
  if (!href) return <span className={className}>{children}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={href}
      className={`underline decoration-ink-600 underline-offset-2 transition-colors hover:decoration-accent hover:text-accent ${className}`}
    >
      {children}
    </a>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="px-4 py-8 text-center text-xs text-ink-400">{children}</p>;
}

export function Button({
  children,
  onClick,
  tone = "neutral",
  disabled,
  title,
}: {
  children: React.ReactNode;
  onClick: () => void;
  tone?: "neutral" | "danger" | "good";
  disabled?: boolean;
  title?: string;
}) {
  const tones = {
    neutral: "border-ink-600 text-ink-300 hover:border-ink-400 hover:text-ink-100",
    danger: "border-danger/40 text-danger hover:border-danger hover:bg-danger/10",
    good: "border-signal/40 text-signal hover:border-signal hover:bg-signal/10",
  } as const;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`rounded border px-2 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${tones[tone]}`}
    >
      {children}
    </button>
  );
}
