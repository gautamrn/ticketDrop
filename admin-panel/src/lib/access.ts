import { auth } from "@/auth";

export async function currentUser() {
  const session = await auth();
  return session?.user ?? null;
}

export async function requireUser() {
  const user = await currentUser();
  if (!user?.id) {
    return { user: null, response: Response.json({ error: "unauthorized" }, { status: 401 }) };
  }
  return { user, response: null };
}

export async function requireOperator() {
  const access = await requireUser();
  if (access.response) return access;
  if (access.user.role !== "operator") {
    return {
      user: null,
      response: Response.json({ error: "forbidden" }, { status: 403 }),
    };
  }
  return access;
}
