# admin-panel

Next.js App Router application for TicketDrop's authenticated user experience
and operator read model.

## User product

- `/app` summarizes active watches and recent alerts.
- `/app/settings` saves one configured RA metro and direct artist or venue
  preferences. Preferences are plain labels written to `user_preferences`.
- `/app/events` lists the enumerated RA catalogue and supports explicit Watch
  and Unwatch. Subscription origin identifies scout-created and explicit rows.
- `/app/alerts` reads personal delivery history from Postgres and shows the
  classified change, status, RA link, seller link, and transition detail.

All product routes validate the server session and row owner. A user may have
at most 25 active subscriptions. Unwatch persists `user_disabled` so automatic
matching does not silently restore the watch.

The alert channel is the email verified by the sign-in provider. The settings
API does not accept an arbitrary address and mark it verified.

## Operator product

`/admin` requires the `operator` role. It reads recent drops, transition detail,
stream/group health, aggregate counts, and the shared watchlist. Its only
pipeline mutation pauses or resumes a shared watch. Recent drops come from
Postgres; Redis is used only for stream health.

## API ownership

```text
GET/PATCH/DELETE /api/preferences    direct artist/venue preference management
GET/PATCH        /api/settings       metro, verified channel, allowed areas
GET/POST/DELETE  /api/subscriptions explicit Watch/Unwatch
GET              /api/events        personal catalogue view
GET              /api/alerts        personal durable alert history
GET              /api/drops         operator drops and transitions
GET/POST         /api/watchlist     operator shared-watch control
GET              /api/health        operator Redis/Postgres health
POST             /api/demo/replay   labeled demo data when enabled
GET              /api/unsubscribe   signed email unsubscribe
```

Account and mutation endpoints are rate-limited. The replay endpoint is
available only when `PUBLIC_DEMO_MODE=true` and is not part of the production
profile.

## Run

```powershell
cd admin-panel
npm ci
npm run dev
```

The app loads the repository-root `.env`; process environment values take
precedence. Local development exposes the demo credentials provider.
Production requires configured GitHub and/or Google OAuth.

Verification:

```powershell
npm run lint
npx tsc --noEmit
npm run build
```

RA and seller URLs are rendered directly. Do not add redirect wrappers or
tracking parameters to alert links.
