import { STATE_TONE } from "@/lib/format";

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2.5 font-semibold tracking-[-0.025em]">
      <span className="relative grid h-7 w-7 place-items-center rounded-full border border-white/12 bg-white/[0.045]">
        <span className="h-2.5 w-2.5 rounded-full bg-accent" />
        <span className="absolute h-4.5 w-px rotate-45 bg-ink-950" />
      </span>
      {!compact && <span>TicketDrop</span>}
    </span>
  );
}

export function ArrowUpRight({ className = "" }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      fill="none"
      className={`h-4 w-4 ${className}`}
    >
      <path d="M4 12 12 4M6 4h6v6" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

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
      className={`surface flex min-h-0 flex-col rounded-2xl ${className}`}
    >
      <header className="flex items-baseline justify-between gap-3 border-b border-white/[0.07] px-5 py-4">
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
      className={`inline-flex shrink-0 items-center rounded-full px-2 py-1 text-[10px] font-semibold tracking-wider ring-1 ring-inset ${tone}`}
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
      className={`inline-flex shrink-0 items-center rounded-full px-2 py-1 text-[10px] font-medium ring-1 ring-inset ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * An outbound link to a ticketing source.
 *
 * The href is rendered exactly as stored: no tracking parameter, shortener, or
 * redirect. `target` and `rel` do not alter the URL itself.
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
      className={`rounded-full border px-3 py-1.5 text-[11px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${tones[tone]}`}
    >
      {children}
    </button>
  );
}
