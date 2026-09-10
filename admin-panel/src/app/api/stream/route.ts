import { blockingRedis, fieldsToObject, STREAM_RESULTS } from "@/lib/redis";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKFILL = 40;
const BLOCK_MS = 15_000;
const HEARTBEAT_MS = 20_000;

/**
 * Server-Sent Events tail of `ticket:drops:results`.
 *
 * ARCHITECTURE.md §2 drew this as a WebSocket. SSE is the better fit and the
 * reason is the traffic shape: this feed is strictly server-to-client. Every
 * operator action — toggling a watch, confirming a link — is a mutation with a
 * response, which is a POST, not a socket frame. Paying for a bidirectional
 * transport buys a channel we would never send on, and costs a custom server,
 * because Next's App Router cannot upgrade a connection inside a route handler.
 *
 * SSE also gets reconnection for free. `EventSource` re-dials with backoff on
 * its own, whereas every production WebSocket eventually grows a hand-written
 * reconnect loop that is subtly wrong about exponential backoff.
 *
 * Each client gets its own Redis connection because `XREAD BLOCK` occupies one
 * for the duration of the block. That is the real cost of this choice: it scales
 * with concurrent operators, not with drop volume. For a single-operator panel
 * that is the correct trade; at a hundred viewers it would need one shared tail
 * fanning out to subscribers.
 */
export async function GET(request: Request) {
  const encoder = new TextEncoder();
  const client = blockingRedis();

  const stream = new ReadableStream({
    async start(controller) {
      let open = true;

      const send = (event: string, data: unknown) => {
        if (!open) return;
        try {
          controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
        } catch {
          open = false;
        }
      };

      const close = () => {
        if (!open) return;
        open = false;
        clearInterval(heartbeat);
        // Disconnect rather than returning to a pool: this socket may be parked
        // in a blocking read that no one is waiting on any more.
        client.disconnect();
        try {
          controller.close();
        } catch {
          /* already closed by the runtime */
        }
      };

      // Proxies and load balancers drop idle connections. A comment frame is
      // not delivered to any EventSource listener, so it keeps the pipe warm
      // without the client having to filter out keepalive noise.
      const heartbeat = setInterval(() => {
        if (!open) return;
        try {
          controller.enqueue(encoder.encode(": keepalive\n\n"));
        } catch {
          close();
        }
      }, HEARTBEAT_MS);

      request.signal.addEventListener("abort", close);

      let cursor = "$";
      try {
        // Backfill first so a freshly opened panel is not blank until the next
        // drop, which during a quiet hour could be a long time.
        const recent = await client.xrevrange(STREAM_RESULTS, "+", "-", "COUNT", BACKFILL);
        const history = recent
          .map(([entry_id, fields]) => ({ entry_id, ...fieldsToObject(fields) }))
          .reverse();
        if (history.length > 0) {
          cursor = history[history.length - 1].entry_id;
        }
        send("backfill", history);
      } catch (err) {
        send("error", { message: (err as Error).message });
      }

      while (open) {
        try {
          const response = await client.xread(
            "BLOCK",
            BLOCK_MS,
            "STREAMS",
            STREAM_RESULTS,
            cursor,
          );
          if (!open) break;
          if (!response) continue; // block expired with nothing new, normal

          for (const [, entries] of response) {
            for (const [entry_id, fields] of entries) {
              cursor = entry_id;
              send("drop", { entry_id, ...fieldsToObject(fields) });
            }
          }
        } catch (err) {
          if (!open) break;
          send("error", { message: (err as Error).message });
          // Back off rather than spinning: a Redis that just refused us will
          // refuse us again immediately, and a hot loop turns one outage into
          // a second one.
          await new Promise((resolve) => setTimeout(resolve, 2000));
        }
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      // nginx buffers proxied responses by default, which defeats streaming.
      "X-Accel-Buffering": "no",
    },
  });
}
