from __future__ import annotations

from conftest import message
from harness.notify.email import EmailError, EmailNotifier
from harness.store import Subscriber


class ScriptedClient:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.keys: list[str] = []

    async def send(self, **kwargs) -> str:
        self.keys.append(kwargs["idempotency_key"])
        if len(self.keys) <= self.failures:
            raise EmailError("temporary", transient=True)
        return "provider-1"

    async def aclose(self) -> None:
        return None


async def test_transient_retries_reuse_idempotency_key(store) -> None:
    client = ScriptedClient(failures=2)
    notifier = EmailNotifier(store, client, "https://app.example", "secret")
    subscriber = Subscriber("u1", "c1", "user@example.com")
    await notifier.notify(
        "0192f0c0-0000-7000-8000-000000000001", subscriber, message(), store.context
    )
    assert len(client.keys) == 3
    assert len(set(client.keys)) == 1
    assert next(iter(store.deliveries.values()))["status"] == "sent"


async def test_sent_delivery_is_not_resent(store) -> None:
    client = ScriptedClient()
    notifier = EmailNotifier(store, client, "https://app.example", "secret")
    subscriber = Subscriber("u1", "c1", "user@example.com")
    drop_id = "0192f0c0-0000-7000-8000-000000000001"
    await notifier.notify(drop_id, subscriber, message(), store.context)
    await notifier.notify(drop_id, subscriber, message(), store.context)
    assert len(client.keys) == 1


async def test_email_contains_links_and_waiting_room(store) -> None:
    notifier = EmailNotifier(store, ScriptedClient(), "https://app.example", "secret")
    subscriber = Subscriber("u1", "c1", "user@example.com")
    event = store.context.__class__(
        store.context.title,
        store.context.metro,
        store.context.venue,
        store.context.starts_at,
        store.context.content_url,
        store.context.ticket_link,
        True,
    )
    body = notifier._text(subscriber, message(), event)
    assert event.content_url in body
    assert event.ticket_link in body
    assert "waiting room" in body
