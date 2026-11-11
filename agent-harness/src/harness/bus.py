"""Redis consumer-group intake and short-lived drop claims."""

from __future__ import annotations

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
    delivery_count: int = 1


class Bus(Protocol):
    async def ensure_group(self) -> None: ...
    async def read(self) -> list[StreamEntry]: ...
    async def reclaim(self) -> list[StreamEntry]: ...
    async def ack(self, entry_id: str) -> None: ...
    async def claim_drop(self, drop_id: str) -> bool: ...


class RedisBus:
    def __init__(
        self,
        client: Redis,
        *,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int,
        batch: int,
        autoclaim_min_idle_ms: int,
        claim_ttl_s: int,
    ) -> None:
        self._r = client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._batch = batch
        self._min_idle_ms = autoclaim_min_idle_ms
        self._claim_ttl_s = claim_ttl_s
        self._autoclaim_cursor = "0-0"

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisBus:
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
            return []
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            await self.ensure_group()
            return []
        return _flatten(response)

    async def reclaim(self) -> list[StreamEntry]:
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
        self._autoclaim_cursor = response[0] or "0-0"
        entries: list[StreamEntry] = []
        for entry_id, fields in response[1]:
            if fields is None:
                await self.ack(entry_id)
                continue
            pending = await self._r.xpending_range(
                name=self._stream,
                groupname=self._group,
                min=entry_id,
                max=entry_id,
                count=1,
            )
            count = int(pending[0].get("times_delivered", 1)) if pending else 1
            entries.append(StreamEntry(entry_id, dict(fields), count))
        return entries

    async def ack(self, entry_id: str) -> None:
        await self._r.xack(self._stream, self._group, entry_id)

    async def claim_drop(self, drop_id: str) -> bool:
        acquired = await self._r.set(
            f"drop:claim:{drop_id}", self._consumer, nx=True, ex=self._claim_ttl_s
        )
        return bool(acquired)


def _flatten(response: Any) -> list[StreamEntry]:
    return [
        StreamEntry(entry_id, dict(fields or {}))
        for _stream, messages in response or []
        for entry_id, fields in messages
    ]
