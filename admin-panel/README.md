# admin-panel

Next.js (App Router) read model over the TicketDrop pipeline. Tails
`ticket:drops:results`, reads the durable record from Postgres, and exposes the
small set of overrides an operator needs when something goes wrong.

It contains no decision logic. Nothing here influences whether a drop is
reserved; that lives entirely in the harness. The only writes are the four
operator actions listed below.

## Run

Needs the compose stack up and, to have anything to display, the ingest service
and harness running.

```powershell
cd admin-panel
npm ci
npm run dev     # http://localhost:3000
```

`next.config.ts` loads the repository-root `.env` so the panel reads the same
`DATABASE_URL` and `REDIS_URL` as everything else. A real environment variable
always wins over the file, so containerised deployment needs no change.

For a production build, run `npm run build` followed by `npm run start`.

The panel has no authentication and includes mutating operator endpoints. Keep
it on localhost unless an authenticated reverse proxy protects it.

## What it shows

| Area | Source | Why it is there |
| --- | --- | --- |
| Live drops | SSE tail of `ticket:drops:results` | The pipeline's output, as it happens |
| Drop detail | `drop_transitions` in Postgres | Per-stage `latency_ms` against the §3 budget |
| Health strip | `XLEN`, `XINFO GROUPS`, aggregate counts | Consumer lag is the first symptom of a harness that cannot keep up |
| Watchlist | `watchlist` ⋈ `events` | Which events are on the 2 s availability loop |
| Preferences | `user_preferences` | Hype scoring fails closed; an empty set silently reserves nothing |
| Cross-source links | `event_links` where `NOT confirmed` | §9 failure 9: low-confidence matches get a human, not an auto-merge |

## Overrides

Four, all narrow:

- **Pause / resume a watch.** Deactivates rather than deletes, so the reason the
  event was promoted survives the operator silencing it.
- **Mute / unmute a preference.**
- **Delete a preference.**
- **Confirm / reject a cross-source link.** Rejecting deletes the row, because
  the resolver only reconsiders events that currently have no link; leaving a
  rejected row would permanently block a correct future match.

**Adding a preference is deliberately not here.** A new preference needs its
label embedded into a 1024-d vector, and the embedder lives in the harness. A
TypeScript reimplementation would put scoring behavior in a service that is
supposed to contain none, and any drift between the two would produce vectors
that silently never match anything. Use the CLI:

```powershell
cd agent-harness
.\.venv\Scripts\python.exe -m harness prefs add --kind artist --label "Prospa"
```

## SSE rather than WebSockets

`ARCHITECTURE.md` §2 drew this edge as a WebSocket. It is Server-Sent Events,
and the reason is the traffic shape: the feed is strictly server-to-client.
Every operator action is a mutation that wants a response, which is a POST. A
bidirectional transport would buy a channel we never send on, and cost a custom
server, since the App Router cannot upgrade a connection inside a route handler.
`EventSource` also reconnects with backoff natively, which is the part of a
hand-rolled WebSocket client that is most often subtly wrong.

The cost is real and worth naming: `XREAD BLOCK` holds one Redis connection per
connected browser, so this scales with concurrent operators rather than with
drop volume. For a single-operator panel that is the right trade. Past roughly a
dozen viewers it needs one shared tail fanning out to subscribers instead.

## Rendering event links

Outbound links are rendered **verbatim** through the `EventLink` component.
EDMTrain's API terms require their event link to be displayed unmodified
(`ARCHITECTURE.md` §10.1), so no tracking parameter, shortener, or redirect may
be added. `target` and `rel` are anchor attributes and do not alter the URL.
