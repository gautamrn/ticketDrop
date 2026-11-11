import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { HealthBar } from "@/components/HealthBar";
import { OperatorDrops } from "@/components/OperatorDrops";
import { OperatorWatchlist } from "@/components/OperatorWatchlist";
import { Brand } from "@/components/ui";

export default async function AdminPage() {
  const session = await auth();
  if (!session?.user) redirect("/signin");
  if (session.user.role !== "operator") redirect("/app");

  return (
    <main className="mx-auto flex min-h-screen max-w-[1600px] flex-col gap-4 p-4 sm:p-6">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-white/[0.07] pb-5">
        <div>
          <Brand />
          <h1 className="mt-4 text-2xl font-medium tracking-[-0.035em]">Operations</h1>
          <p className="mt-1 text-xs text-ink-400">
            Postgres-backed onsale signals, health counts, and shared-watch controls.
          </p>
        </div>
        <a href="/app" className="button-secondary min-h-10 text-xs">
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
