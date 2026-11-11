type Window = { count: number; resetsAt: number };

const globalForLimits = globalThis as unknown as { ticketDropLimits?: Map<string, Window> };
const limits = globalForLimits.ticketDropLimits ?? new Map<string, Window>();
globalForLimits.ticketDropLimits = limits;

export function rateLimit(
  userId: string,
  bucket: string,
  limit = 30,
  windowMs = 60_000,
): Response | null {
  const now = Date.now();
  const key = `${bucket}:${userId}`;
  const current = limits.get(key);
  if (!current || current.resetsAt <= now) {
    limits.set(key, { count: 1, resetsAt: now + windowMs });
    return null;
  }
  current.count += 1;
  if (current.count <= limit) return null;
  return Response.json(
    { error: "too many requests" },
    {
      status: 429,
      headers: { "Retry-After": String(Math.ceil((current.resetsAt - now) / 1000)) },
    },
  );
}
