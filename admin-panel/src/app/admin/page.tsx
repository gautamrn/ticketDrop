import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { HealthBar } from "@/components/HealthBar";
import { OperatorDrops } from "@/components/OperatorDrops";
import { OperatorWatchlist } from "@/components/OperatorWatchlist";

export default async function AdminPage() {
  const session = await auth();
  if (!session?.user) redirect("/signin");
  if (session.user.role !== "operator") redirect("/app");

  return (
    <main className="mx-auto flex min-h-screen max-w-[1600px] flex-col gap-3 p-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">TicketDrop operations</h1>
          <p className="text-xs text-ink-400">
            Postgres-backed onsale signals, health counts, and shared-watch controls.
          </p>
        </div>
        <a href="/app" className="text-xs text-accent hover:underline">
          User app
        </a>
      </header>
      <HealthBar />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[1.6fr_1fr]">
        <OperatorDrops />
        <OperatorWatchlist />
      </div>
    </main>
  );
}
