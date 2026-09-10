"""Redis: consumer group intake, the idempotency claim, and the results stream.

Redis Streams is the only channel between ingestion and the harness. Go is the
only writer to `ticket:drops`; the harness is the only writer to
`ticket:drops:results`. Nothing here reaches back to the ingest service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .log import get

_log = get("harness.bus")


@dataclass(frozen=True, slots=True)
class StreamEntry:
    entry_id: str
    fields: dict[str, str]
    # How many times the group has handed this entry out. 1 on a fresh read;
    # higher only for entries recovered by XAUTOCLAIM from a dead consumer.
    delivery_count: int = 1


class Bus(Protocol):
    async def ensure_group(self) -> None: ...

    async def read(self) -> list[StreamEntry]: ...

    async def reclaim(self) -> list[StreamEntry]: ...

    async def ack(self, entry_id: str) -> None: ...

    async def claim_drop(self, drop_id: str) -> bool: ...

    async def publish_result(self, result: dict[str, Any]) -> None: ...

    async def cached_selector(self, domain: str, logical_name: str) -> str | None: ...

    async def cache_selector(self, domain: str, logical_name: str, selector: str) -> None: ...


class RedisBus:
    def __init__(
        self,
        client: Redis,
        *,
        stream: str,
        result_stream: str,
        result_maxlen: int,
        group: str,
        consumer: str,
        block_ms: int,
        batch: int,
        autoclaim_min_idle_ms: int,
        claim_ttl_s: int,
    ) -> None:
        self._r = client
        self._stream = stream
        self._result_stream = result_stream
        self._result_maxlen = result_maxlen
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._batch = batch
        self._min_idle_ms = autoclaim_min_idle_ms
        self._claim_ttl_s = claim_ttl_s
        self._autoclaim_cursor = "0-0"

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisBus:
        """Socket timeout is derived from the XREADGROUP block, not fixed.

        A blocking read legitimately holds the socket idle for `block_ms`. A
        socket timeout at or below that turns the normal idle case into a
        client-side error on every quiet cycle, which is how an ostensibly
        healthy harness dies five seconds after startup.
        """
        block_ms = int(kwargs.get("block_ms", 5_000))
        client = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=block_ms / 1000.0 + 10.0,
            socket_connect_timeout=5.0,
            health_check_interval=30,
        )
        return cls(client, **kwargs)

    async def ping(self) -> None:
        await self._r.ping()

    async def aclose(self) -> None:
        await self._r.aclose()

    async def ensure_group(self) -> None:
        """Create the group at the stream tail, tolerating a concurrent creator.

        MKSTREAM because the harness legitimately starts before the ingester
        has published anything. `$` rather than `0` so a first-ever start does
        not replay the entire retained backlog as live drops — the same cold
        start reasoning as ARCHITECTURE.md §9 failure 5.
        """
        try:
            await self._r.xgroup_create(self._stream, self._group, id="$", mkstream=True)
            _log.info("consumer group created", stream=self._stream, group=self._group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self) -> list[StreamEntry]:
        try:
            response = await self._r.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream: ">"},
                count=self._batch,
                block=self._block_ms,
            )
        except RedisTimeoutError:
            # An idle blocking read is indistinguishable from a socket timeout
            # at the client. Belt and braces alongside the derived socket
            # timeout above: silence on the stream is the normal state, not a
            # fault, and it must never propagate as one.
            return []
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            # The stream or group was deleted underneath us, which happens when
            # someone flushes Redis in development.
            await self.ensure_group()
            return []
        return _flatten(response)

    async def reclaim(self) -> list[StreamEntry]:
        """XAUTOCLAIM entries abandoned by a dead consumer.

        This is the only recovery path for a worker that died between XREADGROUP
        and XACK. The cursor is kept across calls so a large PEL is walked in
        pages rather than re-scanned from the start every interval.
        """
        try:
            response = await self._r.xautoclaim(
                name=self._stream,
                groupname=self._group,
                consumername=self._consumer,
                min_idle_time=self._min_idle_ms,
                start_id=self._autoclaim_cursor,
                count=self._batch,
            )
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                await self.ensure_group()
                return []
            raise

        cursor, messages = response[0], response[1]
        self._autoclaim_cursor = cursor or "0-0"

        entries: list[StreamEntry] = []
        for entry_id, fields in messages:
            if fields is None:
                # Claimed but since deleted from the stream. Nothing to run.
                await self.ack(entry_id)
                continue
            entries.append(
                StreamEntry(
                    entry_id=entry_id,
                    fields=dict(fields),
                    delivery_count=await self._delivery_count(entry_id),
                )
            )
        return entries

    async def _delivery_count(self, entry_id: str) -> int:
        pending = await self._r.xpending_range(
            name=self._stream, groupname=self._group, min=entry_id, max=entry_id, count=1
        )
        if not pending:
            return 1
        return int(pending[0].get("times_delivered", 1))

    async def ack(self, entry_id: str) -> None:
        await self._r.xack(self._stream, self._group, entry_id)

    async def claim_drop(self, drop_id: str) -> bool:
        """The idempotency gate. Taken before any side effect.

        SET NX is a single round trip and is atomic across workers, which is
        what makes at-least-once delivery survivable: a redelivered message
        after a crash finds the key present and does not re-run the browser
        stage. The TTL, not an explicit release, is what frees it — a worker
        that dies holding a lock must not hold it forever.
        """
        acquired = await self._r.set(
            f"drop:claim:{drop_id}", self._consumer, nx=True, ex=self._claim_ttl_s
        )
        return bool(acquired)

    async def publish_result(self, result: dict[str, Any]) -> None:
        """Append an FSM outcome for the admin panel to tail.

        Values are flattened to strings for the same reason the inbound stream
        is: Redis streams have no nested types. A failure here is logged and
        swallowed, because the outcome is already durable in Postgres and the
        panel is a read model, not the record.
        """
        fields = {k: _flatten_value(v) for k, v in result.items() if v is not None}
        try:
            await self._r.xadd(
                self._result_stream, fields, maxlen=self._result_maxlen, approximate=True
            )
        except Exception as exc:  # noqa: BLE001 - never fail a drop on the read model
            _log.warning("result publish failed", stream=self._result_stream, err=str(exc))

    async def cached_selector(self, domain: str, logical_name: str) -> str | None:
        return await self._r.hget(f"selector_overrides:{domain}", logical_name)

    async def cache_selector(self, domain: str, logical_name: str, selector: str) -> None:
        await self._r.hset(f"selector_overrides:{domain}", logical_name, selector)


def _flatten(response: Any) -> list[StreamEntry]:
    entries: list[StreamEntry] = []
    for _stream, messages in response or []:
        for entry_id, fields in messages:
            entries.append(StreamEntry(entry_id=entry_id, fields=dict(fields or {})))
    return entries


def _flatten_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, default=str)
