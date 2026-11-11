"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  ["/app/discover", "Discover"],
  ["/app/events", "Events"],
  ["/app/alerts", "Alerts"],
  ["/app/settings", "Settings"],
] as const;

export function ProductNav({ operator }: { operator: boolean }) {
  const pathname = usePathname();

  return (
    <nav
      className="order-3 flex w-full items-center gap-1 overflow-x-auto text-sm sm:order-none sm:w-auto"
      aria-label="Product"
    >
      {links.map(([href, label]) => {
        const active = pathname === href;
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={`rounded-full px-3 py-1.5 transition-colors ${
              active
                ? "bg-white/[0.08] text-ink-100"
                : "text-ink-300 hover:bg-white/[0.06] hover:text-ink-100"
            }`}
          >
            {label}
          </Link>
        );
      })}
      {operator && (
        <Link
          className="rounded-full px-3 py-1.5 text-accent hover:bg-accent/8"
          href="/admin"
        >
          Operations
        </Link>
      )}
    </nav>
  );
}
