# Admin panel

Next.js 16 product and operator UI.

## Routes

- `/app` — watch and delivery summary
- `/app/discover` — feed-only metro, genre, and price matching
- `/app/events` — live RA catalogue with explicit Watch/Unwatch
- `/app/alerts` — durable personal signal and delivery history
- `/app/settings` — metro, discovery, and exact artist/venue follows
- `/admin` — operator health, drops, and shared-watch controls

Discovery is read-only until a user presses Watch. Users are capped at 25
active subscriptions, and Unwatch persists `user_disabled` so the scout cannot
silently restore it.

All personal routes validate the server session and row owner. Alerts use the
verified sign-in email; the settings API cannot bless an arbitrary address.
RA and seller URLs are rendered directly without redirect wrappers.

## Develop

```powershell
npm ci
npm run dev

npm test
npm run lint
npm run typecheck
npm run build
```

The app reads the repository-root `.env`. Production requires Google or GitHub
OAuth; `LOCAL_USER_EMAIL` is restricted to the loopback-only local profile.
