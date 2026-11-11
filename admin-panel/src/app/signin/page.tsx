import { redirect } from "next/navigation";
import Link from "next/link";
import { auth, signIn } from "@/auth";
import { Brand } from "@/components/ui";

export default async function SignInPage() {
  const session = await auth();
  if (session?.user) redirect("/app");

  const demoEnabled =
    Boolean(process.env.LOCAL_USER_EMAIL) ||
    process.env.PUBLIC_DEMO_MODE === "true" ||
    process.env.NODE_ENV !== "production";
  const localAccount = Boolean(process.env.LOCAL_USER_EMAIL);

  return (
    <main className="site-shell grid min-h-screen items-center gap-12 py-12 lg:grid-cols-[1.1fr_0.9fr]">
      <section className="max-w-2xl">
        <Link href="/">
          <Brand />
        </Link>
        <p className="eyebrow mt-20">Your private signal desk</p>
        <h1 className="page-title mt-6">Pick up where the catalogue left off.</h1>
        <p className="mt-6 max-w-lg text-lg leading-8 text-ink-400">
          Your metro, followed names, discovery profile, and complete alert audit stay attached to
          your account.
        </p>
      </section>
      <section className="surface w-full rounded-[2rem] p-7 sm:p-10">
        <p className="text-sm text-ink-400">Account access</p>
        <h2 className="mt-2 text-3xl font-medium tracking-[-0.04em]">Sign in to TicketDrop</h2>
        <div className="mt-8 space-y-3">
          {process.env.AUTH_GOOGLE_ID && (
            <form
              action={async () => {
                "use server";
                await signIn("google", { redirectTo: "/app" });
              }}
            >
              <button className="button-primary w-full">
                Continue with Google
              </button>
            </form>
          )}
          {process.env.AUTH_GITHUB_ID && (
            <form
              action={async () => {
                "use server";
                await signIn("github", { redirectTo: "/app" });
              }}
            >
              <button className="button-secondary w-full">
                Continue with GitHub
              </button>
            </form>
          )}
          {demoEnabled && (
            <form
              action={async () => {
                "use server";
                await signIn("local", { redirectTo: "/app" });
              }}
            >
              <button className="button-secondary w-full border-accent/30 text-accent">
                {localAccount ? "Open local account" : "Open local demo"}
              </button>
            </form>
          )}
        </div>
        <p className="mt-8 border-t border-white/[0.07] pt-5 text-xs leading-5 text-ink-400">
          TicketDrop stores preferences and delivery state in Postgres. It never stores ticketing
          credentials or enters a seller flow.
        </p>
      </section>
    </main>
  );
}
