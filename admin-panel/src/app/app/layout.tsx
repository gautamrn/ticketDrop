import Link from "next/link";
import { redirect } from "next/navigation";
import { auth, signOut } from "@/auth";

export default async function ProductLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  if (!session?.user) redirect("/signin");

  return (
    <main className="mx-auto min-h-screen max-w-6xl px-4 py-5 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-ink-800 pb-4">
        <Link href="/app" className="font-semibold tracking-tight">
          TicketDrop
        </Link>
        <nav className="flex items-center gap-1 text-sm" aria-label="Product">
          <Link className="rounded px-3 py-2 hover:bg-ink-850" href="/app/events">
            Events
          </Link>
          <Link className="rounded px-3 py-2 hover:bg-ink-850" href="/app/alerts">
            Alerts
          </Link>
          <Link className="rounded px-3 py-2 hover:bg-ink-850" href="/app/settings">
            Settings
          </Link>
          {session.user.role === "operator" && (
            <Link className="rounded px-3 py-2 text-accent hover:bg-ink-850" href="/admin">
              Ops
            </Link>
          )}
        </nav>
        <form
          action={async () => {
            "use server";
            await signOut({ redirectTo: "/" });
          }}
        >
          <button className="text-xs text-ink-400 hover:text-ink-100">Sign out</button>
        </form>
      </header>
      {children}
    </main>
  );
}
