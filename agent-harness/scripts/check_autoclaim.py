"""Verify XAUTOCLAIM recovery against live Redis, on a throwaway stream.

The unit tests fake the client, so the reclaim path's Redis semantics — what
XAUTOCLAIM actually returns, and whether the delivery count increments — are
only ever really proven here. It is the sole recovery path for a worker that
died between XREADGROUP and XACK, so it is worth proving rather than assuming.

Uses its own stream and group so it cannot disturb ticket:drops.
"""

from __future__ import annotations

import asyncio
import sys

from redis.asyncio import Redis

from harness import config
from harness.bus import RedisBus

STREAM = "ticket:drops:autoclaim-check"
GROUP = "tdrop-harness-check"


async def main() -> int:
    cfg = config.load()
    admin = Redis.from_url(cfg.redis_url, decode_responses=True)
    await admin.delete(STREAM)

    common = {
        "stream": STREAM,
        "result_stream": f"{STREAM}:results",
        "result_maxlen": 100,
        "group": GROUP,
        "block_ms": 100,
        "batch": 8,
        # Zero so the check does not have to wait 60 s to prove the mechanism.
        "autoclaim_min_idle_ms": 0,
        "claim_ttl_s": 60,
    }
    dead = RedisBus.from_url(cfg.redis_url, consumer="worker-that-dies", **common)
    live = RedisBus.from_url(cfg.redis_url, consumer="worker-that-lives", **common)

    ok = True
    try:
        await dead.ensure_group()
        entry_id = await admin.xadd(STREAM, {"drop_id": "check-1", "change": "new_ticketed"})

        first = await dead.read()
        print(f"first read by dead consumer: {[e.entry_id for e in first]}")
        if len(first) != 1:
            print("FAIL: entry was not delivered")
            ok = False

        # No XACK. The consumer is now pretending to be dead.
        reclaimed = await live.reclaim()
        print(
            "reclaimed by live consumer: "
            f"{[(e.entry_id, e.delivery_count, e.fields.get('drop_id')) for e in reclaimed]}"
        )
        if len(reclaimed) != 1 or reclaimed[0].entry_id != entry_id:
            print("FAIL: XAUTOCLAIM did not hand the entry over")
            ok = False
        elif reclaimed[0].delivery_count < 2:
            print(f"FAIL: expected delivery_count >= 2, got {reclaimed[0].delivery_count}")
            ok = False

        await live.ack(entry_id)
        pending = await admin.xpending(STREAM, GROUP)
        print(f"pending after ack: {pending['pending']}")
        if pending["pending"] != 0:
            print("FAIL: entry still pending after ack")
            ok = False
    finally:
        await admin.delete(STREAM, f"{STREAM}:results")
        await dead.aclose()
        await live.aclose()
        await admin.aclose()

    print("\nXAUTOCLAIM:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
