from __future__ import annotations

from harness.store import PgStore


class _Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeConnection(_Context):
    def __init__(self) -> None:
        self.sql = ""

    def transaction(self):
        return _Context()

    async def execute(self, _sql: str):
        return None

    async def fetchval(self, sql: str):
        self.sql = sql
        return 0


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self):
        return self.conn


async def test_scout_never_reactivates_user_disabled_subscription() -> None:
    conn = FakeConnection()
    store = PgStore(FakePool(conn))  # type: ignore[arg-type]
    assert await store.scout_subscriptions() == 0
    assert "NOT EXISTS" in conn.sql
    assert "FROM event_subscriptions es" in conn.sql
    assert "ON CONFLICT (user_id, event_id) DO NOTHING" in conn.sql
    assert "DO UPDATE" not in conn.sql


async def test_subscriber_query_excludes_user_disabled() -> None:
    conn = FakeConnection()

    async def fetch(_sql: str, *_args):
        conn.sql = _sql
        return []

    conn.fetch = fetch  # type: ignore[attr-defined]
    store = PgStore(FakePool(conn))  # type: ignore[arg-type]
    assert await store.subscribers("11111111-1111-4111-8111-111111111111") == []
    assert "NOT s.user_disabled" in conn.sql
