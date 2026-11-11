import Link from "next/link";

export default function Page() {
  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col px-6 py-8">
      <nav className="flex items-center justify-between">
        <span className="font-semibold tracking-tight">TicketDrop</span>
        <Link href="/signin" className="rounded-md border border-ink-700 px-3 py-1.5 text-sm">
          Sign in
        </Link>
      </nav>
      <section className="grid flex-1 items-center gap-12 py-20 lg:grid-cols-[1.15fr_0.85fr]">
        <div>
          <p className="mb-4 text-xs font-semibold uppercase tracking-[0.18em] text-accent">
            Resident Advisor onsale signals
          </p>
          <h1 className="max-w-3xl text-4xl font-semibold tracking-tight sm:text-6xl">
            Follow artists in your metro. Know when the sale state changes.
          </h1>
          <p className="mt-6 max-w-2xl text-lg leading-8 text-ink-300">
            TicketDrop watches each show once, filters noisy updates, and emails RA&apos;s listing
            plus the seller link when a classified onsale signal appears. No checkout automation.
            No queue bypass.
          </p>
          <div className="mt-8 flex gap-3">
            <Link href="/signin" className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white">
              Start watching
            </Link>
            <a
              href="https://github.com/gautamrn/ticketDrop"
              className="rounded-md border border-ink-700 px-4 py-2 text-sm"
            >
              View source
            </a>
          </div>
        </div>
        <div className="rounded-xl border border-ink-700 bg-ink-900 p-5">
          <p className="text-xs uppercase tracking-wider text-ink-400">How it works</p>
          <ol className="mt-5 space-y-5">
            {[
              ["01", "Follow artists and venues", "Exact-name matches in your selected metro become watched shows."],
              ["02", "One shared poll", "Duplicate subscribers never create duplicate source requests."],
              ["03", "Classify the delta", "Only ticket, presale, resale, cost, or listing changes alert."],
              ["04", "Email and audit", "Delivery and the classified transition are durable in Postgres."],
            ].map(([number, title, detail]) => (
              <li key={number} className="grid grid-cols-[2rem_1fr] gap-3">
                <span className="font-mono text-xs text-accent">{number}</span>
                <div>
                  <p className="text-sm font-medium">{title}</p>
                  <p className="mt-1 text-xs leading-5 text-ink-400">{detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>
      <footer className="border-t border-ink-800 py-5 text-xs text-ink-400">
        Alert-only by design. Live public operation is gated on current source terms.
      </footer>
    </main>
  );
}
