# 90-second TicketDrop demo

## Setup

Run the RA mock, ingest service, Python worker, and Next.js app. Use a demo user
with metro 19, artist preference `Prospa`, and a verified email channel. Keep
the events page, alerts page, test inbox or email log, and operator page ready.

## Script

**0–15 seconds — source constraint**

"RA has no onsale webhook, and the ticket array is empty without privileged
access. TicketDrop fingerprints the event-level sale fields that are actually
observable."

Show the watched event and its `scout` origin.

**15–30 seconds — silent baseline**

"The first poll stores a baseline and emits nothing. Otherwise every restart
would report the whole watchlist as new."

Show that no alert exists before mutation.

**30–45 seconds — classified change**

```powershell
curl.exe -X POST "http://localhost:8098/admin/presale?id=90001"
```

Refresh Alerts and show `tier_available`, the field-level detail, and both
unmodified links.

**45–60 seconds — delivery**

Open the email. Point out that TicketDrop stops at information and links; it
does not open seller pages or interact with a waiting room.

**60–75 seconds — shared polling**

Show the operator watchlist. Explain that many subscriber rows reconcile to one
RA poll row, so account growth does not multiply requests for the same show.

**75–90 seconds — failure model**

"Redis delivery is at least once. Postgres allows one drop per event
fingerprint and one delivery per drop, user, and channel. Every email retry uses
the same provider idempotency key."

## Recording checklist

- Use 1440p or higher and keep labels readable.
- Keep all synthetic rows visibly labeled `[Demo]`.
- Blur addresses, cookies, credentials, and local environment values.
- Show one clear alert path rather than every control.
- Do not present the 5-second poll interval as stream-to-email latency.
- Do not call a target a measured percentile.
