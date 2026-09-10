import { DropFeed } from "@/components/DropFeed";
import { HealthBar } from "@/components/HealthBar";
import { LinksPanel, PreferencesPanel, WatchlistPanel } from "@/components/ControlPanels";

export default function Page() {
  return (
    <main className="mx-auto flex h-screen max-w-[1600px] flex-col gap-3 p-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">TicketDrop</h1>
          <p className="text-xs text-ink-400">
            Read model over the pipeline. Writes here are operator overrides only — no decision
            logic lives in this service.
          </p>
        </div>
        <p className="hidden text-[11px] text-ink-400 md:block">
          ingest → <code className="text-ink-300">ticket:drops</code> → harness →{" "}
          <code className="text-ink-300">ticket:drops:results</code>
        </p>
      </header>

      <HealthBar />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[1.6fr_1fr]">
        <DropFeed />
        <div className="grid min-h-0 grid-rows-3 gap-3">
          <WatchlistPanel />
          <PreferencesPanel />
          <LinksPanel />
        </div>
      </div>
    </main>
  );
}
