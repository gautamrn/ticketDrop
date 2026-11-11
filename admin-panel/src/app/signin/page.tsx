import { redirect } from "next/navigation";
import Link from "next/link";
import { auth, signIn } from "@/auth";

export default async function SignInPage() {
  const session = await auth();
  if (session?.user) redirect("/app");

  const demoEnabled =
    process.env.PUBLIC_DEMO_MODE === "true" || process.env.NODE_ENV !== "production";

  return (
    <main className="mx-auto flex min-h-screen max-w-md items-center px-6">
      <div className="w-full rounded-xl border border-ink-700 bg-ink-900 p-6">
        <Link href="/" className="text-xs text-ink-400 hover:text-ink-100">
          ← TicketDrop
        </Link>
        <h1 className="mt-6 text-2xl font-semibold">Sign in</h1>
        <p className="mt-2 text-sm text-ink-400">
          Your metro, followed artists and venues, and alert history are private to your account.
        </p>
        <div className="mt-6 space-y-3">
          {process.env.AUTH_GOOGLE_ID && (
            <form
              action={async () => {
                "use server";
                await signIn("google", { redirectTo: "/app" });
              }}
            >
              <button className="w-full rounded-md bg-ink-100 px-4 py-2 text-sm font-medium text-ink-950">
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
              <button className="w-full rounded-md border border-ink-600 px-4 py-2 text-sm">
                Continue with GitHub
              </button>
            </form>
          )}
          {demoEnabled && (
            <form
              action={async () => {
                "use server";
                await signIn("demo", { redirectTo: "/app" });
              }}
            >
              <button className="w-full rounded-md border border-accent px-4 py-2 text-sm text-accent">
                Open local demo
              </button>
            </form>
          )}
        </div>
      </div>
    </main>
  );
}
