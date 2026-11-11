import Link from "next/link";
import { ArrowUpRight, Brand } from "@/components/ui";

export default function Page() {
  return (
    <main className="overflow-hidden">
      <nav className="site-shell flex h-20 items-center justify-between border-b border-white/[0.07]">
        <Brand />
        <div className="flex items-center gap-2">
          <a
            href="https://github.com/gautamrn/ticketDrop"
            className="hidden rounded-full px-4 py-2 text-sm text-ink-300 transition-colors hover:text-ink-100 sm:inline-flex"
          >
            GitHub
          </a>
          <Link href="/signin" className="button-secondary min-h-10 px-4">
            Sign in
          </Link>
        </div>
      </nav>

      <section className="site-shell relative pb-24 pt-20 sm:pb-32 sm:pt-28">
        <div className="enter">
          <p className="eyebrow">Resident Advisor, without refresh anxiety</p>
          <h1 className="display mt-7 max-w-6xl">
            Tickets move fast.
            <br />
            Your signal should too.
          </h1>
          <div className="mt-10 grid items-end gap-8 border-t border-white/[0.08] pt-8 md:grid-cols-[1fr_auto]">
            <p className="max-w-2xl text-lg leading-8 text-ink-300 sm:text-xl">
              Discover Detroit events, follow the ones that matter, and get one audited alert when
              Resident Advisor shows a real onsale change.
            </p>
            <div className="flex flex-wrap gap-3">
              <Link href="/signin" className="button-primary">
                Open TicketDrop
              </Link>
              <a
                href="#signal"
                className="button-secondary"
              >
                See the signal path
              </a>
            </div>
          </div>
        </div>
      </section>

      <section id="signal" className="site-shell pb-24 sm:pb-36">
        <div className="surface overflow-hidden rounded-[2rem]">
          <div className="flex flex-wrap items-center justify-between gap-4 border-b border-white/[0.07] px-6 py-5 sm:px-8">
            <div>
              <p className="eyebrow">Signal path</p>
              <p className="mt-1 text-sm text-ink-400">A change has to earn the alert.</p>
            </div>
            <span className="inline-flex items-center gap-2 rounded-full border border-signal/20 bg-signal/8 px-3 py-1.5 text-xs text-signal">
              <span className="signal-dot h-1.5 w-1.5 rounded-full bg-signal" />
              Live catalogue
            </span>
          </div>
          <ol className="grid lg:grid-cols-4">
            {[
              ["01", "Baseline", "The first observation is stored silently. Cold starts never become false alerts."],
              ["02", "Observe", "One shared poll watches each event, regardless of how many people follow it."],
              ["03", "Classify", "Presale, ticketed, resale, and price changes are separated from noisy edits."],
              ["04", "Deliver", "A stable drop ID makes retries safe across Redis, Postgres, and email."],
            ].map(([number, title, detail], index) => (
              <li
                key={number}
                className={`min-h-64 p-6 sm:p-8 ${
                  index > 0 ? "border-t border-white/[0.07] lg:border-l lg:border-t-0" : ""
                }`}
              >
                <span className="font-mono text-xs text-ink-400">{number}</span>
                <h2 className="mt-16 text-2xl font-medium tracking-[-0.03em]">{title}</h2>
                <p className="mt-3 text-sm leading-6 text-ink-400">{detail}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section className="border-y border-white/[0.07] bg-white/[0.018]">
        <div className="site-shell grid gap-12 py-24 sm:py-32 lg:grid-cols-[0.8fr_1.2fr] lg:gap-24">
          <div>
            <p className="eyebrow">Built for trust</p>
            <h2 className="page-title mt-6">Fast is useful. Explainable is better.</h2>
          </div>
          <div className="divide-y divide-white/[0.08]">
            {[
              ["Discovery without spam", "Genre and budget matches stay in your feed. Email begins only when you deliberately watch an event."],
              ["No duplicate noise", "Database uniqueness and provider idempotency suppress repeated alerts when workers retry."],
              ["No purchase automation", "TicketDrop sends the original links. It never enters queues, solves CAPTCHAs, or checks out."],
            ].map(([title, detail]) => (
              <article key={title} className="grid gap-3 py-7 sm:grid-cols-[13rem_1fr]">
                <h3 className="font-medium">{title}</h3>
                <p className="text-sm leading-6 text-ink-400">{detail}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="site-shell py-24 sm:py-32">
        <div className="flex flex-col items-start justify-between gap-8 sm:flex-row sm:items-end">
          <div>
            <p className="eyebrow">Detroit is live</p>
            <h2 className="mt-5 max-w-3xl text-4xl font-semibold tracking-[-0.045em] sm:text-6xl">
              Find the night. Watch the drop.
            </h2>
          </div>
          <Link href="/signin" className="button-primary gap-2">
            Start discovering
            <ArrowUpRight />
          </Link>
        </div>
      </section>

      <footer className="site-shell flex flex-col gap-3 border-t border-white/[0.08] py-8 text-xs text-ink-400 sm:flex-row sm:items-center sm:justify-between">
        <Brand />
        <p>RA-only. Alert-only. Built to observe, never to purchase.</p>
        <a href="https://github.com/gautamrn/ticketDrop" className="hover:text-ink-100">
          Source on GitHub
        </a>
      </footer>
    </main>
  );
}
