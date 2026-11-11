from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx

from ..log import get
from ..messages import DropMessage
from ..store import AlertContext, Store, Subscriber

_log = get("harness.notify.email")

_CHANGE_COPY = {
    "new_ticketed": "RA now marks this event as ticketed.",
    "tier_available": "A presale opened or the listed ticket cost changed.",
    "returned_to_sale": "RA now marks resale tickets as available.",
}


class EmailError(RuntimeError):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class EmailClient(Protocol):
    async def send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html_body: str,
        idempotency_key: str,
    ) -> str: ...
    async def aclose(self) -> None: ...


class ResendClient:
    def __init__(self, api_key: str, from_email: str, *, timeout_s: float = 5.0) -> None:
        self._from = from_email
        self._client = httpx.AsyncClient(
            base_url="https://api.resend.com",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )

    async def send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html_body: str,
        idempotency_key: str,
    ) -> str:
        try:
            response = await self._client.post(
                "/emails",
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "from": self._from,
                    "to": [to],
                    "subject": subject,
                    "text": text,
                    "html": html_body,
                },
            )
        except httpx.HTTPError as exc:
            raise EmailError(str(exc), transient=True) from exc
        if response.status_code >= 400:
            raise EmailError(
                f"Resend returned {response.status_code}: {response.text[:500]}",
                transient=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            message_id = response.json().get("id")
        except ValueError as exc:
            raise EmailError("Resend returned invalid JSON", transient=True) from exc
        if not message_id:
            raise EmailError("Resend response omitted id", transient=True)
        return str(message_id)

    async def aclose(self) -> None:
        await self._client.aclose()


class LogEmailClient:
    async def send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html_body: str,
        idempotency_key: str,
    ) -> str:
        _log.info("email logged (RESEND_API_KEY unset)", to=to, subject=subject)
        return f"log:{idempotency_key}"

    async def aclose(self) -> None:
        return None


@dataclass(slots=True)
class EmailNotifier:
    store: Store
    client: EmailClient
    app_base_url: str
    unsubscribe_secret: str
    max_attempts: int = 3

    async def notify(
        self,
        drop_id: str,
        subscriber: Subscriber,
        msg: DropMessage,
        event: AlertContext,
    ) -> None:
        delivery = await self.store.prepare_email_delivery(drop_id, subscriber)
        if delivery is None or delivery.status == "sent":
            return
        remaining = max(0, self.max_attempts - delivery.attempts)
        for offset in range(remaining):
            await self.store.mark_delivery_sending(delivery.delivery_id)
            try:
                provider_id = await self.client.send(
                    to=subscriber.email,
                    subject=f"TicketDrop: {event.title}",
                    text=self._text(subscriber, msg, event),
                    html_body=self._html(subscriber, msg, event),
                    idempotency_key=f"drop-{drop_id}-user-{subscriber.user_id}-email",
                )
            except EmailError as exc:
                final = not exc.transient or offset == remaining - 1
                delay = None if final else 0.25 * (2**offset)
                await self.store.mark_delivery_failed(
                    delivery.delivery_id, str(exc), retry_in_seconds=delay
                )
                if final:
                    _log.error(
                        "email delivery failed",
                        drop_id=drop_id,
                        user_id=subscriber.user_id,
                        err=str(exc),
                    )
                    return
                await asyncio.sleep(delay)
                continue
            await self.store.mark_delivery_sent(delivery.delivery_id, provider_id)
            return

    def _unsubscribe_url(self, subscriber: Subscriber) -> str:
        payload = f"{subscriber.user_id}:{subscriber.channel_id}"
        signature = hmac.new(
            self.unsubscribe_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        query = urlencode(
            {"user": subscriber.user_id, "channel": subscriber.channel_id, "sig": signature}
        )
        return f"{self.app_base_url}/api/unsubscribe?{query}"

    def _lines(self, subscriber: Subscriber, msg: DropMessage, event: AlertContext) -> list[str]:
        lines = [
            _CHANGE_COPY[msg.change],
            f"Metro: {event.metro or 'Unknown'}",
            f"Venue: {event.venue or 'Unknown'}",
            f"Starts: {event.starts_at.isoformat() if event.starts_at else 'Unknown'}",
            f"RA event: {event.content_url}",
        ]
        if event.ticket_link:
            lines.append(f"Seller: {event.ticket_link}")
        if event.queue_it:
            lines.append("A waiting room is present; TicketDrop will not navigate it.")
        lines.extend(["", f"Unsubscribe: {self._unsubscribe_url(subscriber)}"])
        return lines

    def _text(self, subscriber: Subscriber, msg: DropMessage, event: AlertContext) -> str:
        return "\n".join(self._lines(subscriber, msg, event))

    def _html(self, subscriber: Subscriber, msg: DropMessage, event: AlertContext) -> str:
        items = "".join(
            f"<li>{html.escape(line)}</li>" for line in self._lines(subscriber, msg, event)
        )
        return f"<h1>{html.escape(event.title)}</h1><ul>{items}</ul>"
