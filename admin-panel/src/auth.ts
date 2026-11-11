import PostgresAdapter from "@auth/pg-adapter";
import NextAuth from "next-auth";
import type { NextAuthConfig } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import GitHub from "next-auth/providers/github";
import Google from "next-auth/providers/google";
import { db, query } from "@/lib/db";

type AppUser = {
  id: string;
  email: string | null;
  name: string | null;
  role: "user" | "operator";
};

const providers: NextAuthConfig["providers"] = [];

if (process.env.AUTH_GITHUB_ID && process.env.AUTH_GITHUB_SECRET) {
  providers.push(
    GitHub({
      clientId: process.env.AUTH_GITHUB_ID,
      clientSecret: process.env.AUTH_GITHUB_SECRET,
    }),
  );
}

if (process.env.AUTH_GOOGLE_ID && process.env.AUTH_GOOGLE_SECRET) {
  providers.push(
    Google({
      clientId: process.env.AUTH_GOOGLE_ID,
      clientSecret: process.env.AUTH_GOOGLE_SECRET,
    }),
  );
}

if (process.env.PUBLIC_DEMO_MODE === "true" || process.env.NODE_ENV !== "production") {
  providers.push(
    Credentials({
      id: "demo",
      name: "Local demo",
      credentials: {},
      async authorize() {
        const email = "demo@local.invalid";
        const rows = await query<AppUser>(
          `INSERT INTO users (email, name)
           VALUES ($1, 'Demo user')
           ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
           RETURNING id::text, email, name, role::text AS role`,
          [email],
        );
        return rows[0] ?? null;
      },
    }),
  );
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  adapter: PostgresAdapter(db()),
  providers,
  session: { strategy: "jwt" },
  pages: { signIn: "/signin" },
  callbacks: {
    async jwt({ token, user }) {
      if (user?.id) {
        token.userId = user.id;
        const rows = await query<{ role: "user" | "operator" }>(
          "SELECT role::text AS role FROM users WHERE id = $1::uuid",
          [user.id],
        );
        token.role = rows[0]?.role ?? "user";
      }
      return token;
    },
    session({ session, token }) {
      if (session.user) {
        session.user.id = String(token.userId ?? token.sub ?? "");
        session.user.role = token.role === "operator" ? "operator" : "user";
      }
      return session;
    },
  },
});
