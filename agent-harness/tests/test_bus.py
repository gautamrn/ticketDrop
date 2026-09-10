"""RedisBus against a stub client.

The redis-py client itself is faked. What is being tested is the translation
between Redis' semantics and the harness', which is where the bugs live: an
idle blocking read, a group that already exists, a claimed-then-deleted entry.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from harness.bus import RedisBus


class StubRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.read_raises: Exception | None = None
        self.read_response: Any = []
        self.autoclaim_response: Any = ("0-0", [], [])
        self.pending: list[dict[str, Any]] = []
        self.group_error: Exception | None = None
        self.set_result = True
        self.hashes: dict[str, dict[str, str]] = {}
        self.added: list[tuple[str, dict[str, str]]] = []
        self.acked: list[str] = []

    async def xgroup_create(self, name, groupname, id=None, mkstream=False):
        self.calls.append(("xgroup_create", (name, groupname, id)))
        if self.group_error:
            raise self.group_error

    async def xreadgroup(self, **kwargs):
        self.calls.append(("xreadgroup", ()))
        if self.read_raises:
            raise self.read_raises
        return self.read_response

    async def xautoclaim(self, **kwargs):
        return self.autoclaim_response

    async def xpending_range(self, **kwargs):
        return self.pending

    async def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)

    async def set(self, key, value, nx=False, ex=None):
        self.calls.append(("set", (key, value, nx, ex)))
        return self.set_result

    async def xadd(self, stream, fields, maxlen=None, approximate=True):
        self.added.append((stream, fields))
        return "1-0"

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value


def make_bus(client: StubRedis, **overrides: Any) -> RedisBus:
    kwargs: dict[str, Any] = {
        "stream": "ticket:drops",
        "result_stream": "ticket:drops:results",
        "result_maxlen": 1000,
        "group": "tdrop-harness",
        "consumer": "test",
        "block_ms": 5000,
        "batch": 8,
        "autoclaim_min_idle_ms": 60_000,
        "claim_ttl_s": 900,
    }
    kwargs.update(overrides)
    return RedisBus(client, **kwargs)


async def test_idle_blocking_read_is_not_a_fault():
    """A blocking XREADGROUP with nothing to return looks like a socket timeout
    at the client. Propagating it kills the harness after one quiet cycle."""
    client = StubRedis()
    client.read_raises = RedisTimeoutError("Timeout reading from localhost:6379")

    assert await make_bus(client).read() == []


async def test_missing_group_is_recreated_rather_than_raised():
    client = StubRedis()
    client.read_raises = ResponseError("NOGROUP No such consumer group")

    assert await make_bus(client).read() == []
    assert any(call[0] == "xgroup_create" for call in client.calls)


async def test_other_response_errors_still_propagate():
    client = StubRedis()
    client.read_raises = ResponseError("WRONGTYPE")

    with pytest.raises(ResponseError):
        await make_bus(client).read()


async def test_existing_group_is_tolerated():
    client = StubRedis()
    client.group_error = ResponseError("BUSYGROUP Consumer Group name already exists")

    await make_bus(client).ensure_group()


async def test_read_flattens_the_stream_response():
    client = StubRedis()
    client.read_response = [("ticket:drops", [("1-0", {"drop_id": "a"})])]

    [entry] = await make_bus(client).read()

    assert entry.entry_id == "1-0"
    assert entry.fields == {"drop_id": "a"}
    assert entry.delivery_count == 1


async def test_reclaim_reports_the_delivery_count():
    client = StubRedis()
    client.autoclaim_response = ("5-0", [("2-0", {"drop_id": "b"})], [])
    client.pending = [{"times_delivered": 3}]

    [entry] = await make_bus(client).reclaim()

    assert entry.delivery_count == 3


async def test_reclaim_acks_an_entry_that_no_longer_exists():
    client = StubRedis()
    client.autoclaim_response = ("5-0", [("2-0", None)], [])

    assert await make_bus(client).reclaim() == []
    assert client.acked == ["2-0"]


async def test_claim_uses_set_nx_with_a_ttl():
    client = StubRedis()

    assert await make_bus(client).claim_drop("drop-1") is True

    key, _value, nx, ex = next(args for name, args in client.calls if name == "set")
    assert key == "drop:claim:drop-1"
    assert nx is True
    assert ex == 900


async def test_claim_returns_false_when_the_key_exists():
    client = StubRedis()
    client.set_result = None

    assert await make_bus(client).claim_drop("drop-1") is False


async def test_results_are_flattened_to_strings():
    client = StubRedis()

    await make_bus(client).publish_result(
        {
            "drop_id": "a",
            "hype_score": 0.9,
            "queue_it": False,
            "components": {"artist": 1.0},
            "error": None,
        }
    )

    _stream, fields = client.added[0]
    assert fields["hype_score"] == "0.9"
    assert fields["queue_it"] == "false"
    assert fields["components"] == '{"artist": 1.0}'
    assert "error" not in fields


async def test_a_failing_result_publish_never_fails_the_drop():
    class ExplodingRedis(StubRedis):
        async def xadd(self, *args, **kwargs):
            raise RuntimeError("stream is full")

    await make_bus(ExplodingRedis()).publish_result({"drop_id": "a"})
