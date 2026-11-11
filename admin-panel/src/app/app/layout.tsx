import Link from "next/link";
import { redirect } from "next/navigation";
import { auth, signOut } from "@/auth";
import { ProductNav } from "@/components/ProductNav";
import { Brand } from "@/components/ui";

export default async function ProductLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  if (!session?.user) redirect("/signin");

  return (
    <main className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-white/[0.07] bg-ink-950/80 backdrop-blur-2xl">
        <div className="site-shell flex min-h-16 flex-wrap items-center gap-4 py-3">
          <Link href="/app" className="mr-auto">
            <Brand />
          </Link>
          <ProductNav operator={session.user.role === "operator"} />
          <div className="flex items-center gap-2">
            <span
              className="grid h-8 w-8 place-items-center rounded-full border border-white/10 bg-white/[0.05] text-xs font-semibold text-ink-300"
              title={session.user.email ?? "Account"}
            >
              {(session.user.name ?? session.user.email ?? "T").charAt(0).toUpperCase()}
            </span>
            <form
              action={async () => {
                "use server";
                await signOut({ redirectTo: "/" });
              }}
            >
              <button className="rounded-full px-3 py-1.5 text-xs text-ink-400 transition-colors hover:bg-white/[0.05] hover:text-ink-100">
                Sign out
              </button>
            </form>
          </div>
        </div>
      </header>
      <div className="site-shell">{children}</div>
    </main>
  );
}
