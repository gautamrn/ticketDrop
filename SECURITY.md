# Security policy

## Supported version

Security fixes target the current `main` branch. TicketDrop is pre-1.0; older
release lines do not receive fixes.

## Reporting a vulnerability

Do not open a public issue containing a credential, exploit, private event, or
user record. Use GitHub private vulnerability reporting. If it is unavailable,
contact the repository owner through the email on their GitHub profile and
include the affected commit, sanitized reproduction steps, expected impact, and
known workaround.

Acknowledgement is best effort within seven days. This is not a monitored
security service.

## Deployment assumptions

A public deployment must:

1. validate identity, role, ownership, and request limits on every protected
   page and API mutation;
2. expose only the TLS reverse proxy, never Postgres, Redis, or service health
   ports;
3. use unique production database, Redis, Auth.js, OAuth, email, and
   unsubscribe secrets supplied through the environment;
4. disable the local credentials provider and keep replay mode out of the
   production profile;
5. use the OAuth-verified address for alerts rather than trusting a submitted
   destination;
6. back up Postgres off-host and test restoration; and
7. confirm current RA terms before enabling live public polling.

Never commit `.env`, source credentials, OAuth secrets, email-provider keys,
cookies, session tokens, unsubscribe secrets, or database dumps. Rotate a
leaked secret even after removing it from the working tree or Git history.

## Product safety boundary

TicketDrop sends sale-state information and links only. It does not open seller
pages, enter waiting rooms, solve CAPTCHAs, submit payments, or evade account
and purchase limits. `queueItEnabled` adds a warning to the email; it does not
cause the system to interact with the queue.

Use labeled replay data when live polling is not authorized. Do not add another
source, seller-page fetching, or purchase interaction without a separate legal,
security, and architecture review.

## Data and delivery

Postgres is the durable record. Redis is an at-least-once transport and cache,
so correctness must not depend on a claim key surviving. Unique drop and
delivery constraints plus the stable email idempotency key protect redelivery.

Email unsubscribe links are HMAC-signed with `UNSUBSCRIBE_SECRET`. Treat that
secret as production-critical: rotation invalidates outstanding links, while
exposure permits forged unsubscribe requests.
