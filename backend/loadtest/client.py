"""N concurrent virtual users hitting a running loadtest.server instance for
a fixed duration with a realistic mixed read/write workload (roughly 85%
reads, 15% writes: browse inventory/activity/expenses, occasionally issue
stock or submit an expense).

Usage:
    python loadtest/client.py --base-url http://localhost:8000 --duration 30
"""
import argparse
import asyncio
import random
import time
from pathlib import Path

import httpx


async def one_user_loop(client: httpx.AsyncClient, base_url: str, user_id: str, item_ids: list[str], stop_at: float, latencies: list, errors: list, lock: asyncio.Lock) -> None:
    headers = {"x-test-user-id": user_id}
    while time.monotonic() < stop_at:
        roll = random.random()
        start = time.monotonic()
        try:
            if roll < 0.35:
                resp = await client.get(f"{base_url}/api/items", headers=headers)
            elif roll < 0.65:
                resp = await client.get(f"{base_url}/api/movements", headers=headers)
            elif roll < 0.85:
                resp = await client.get(f"{base_url}/api/expenses", headers=headers)
            elif roll < 0.95:
                resp = await client.post(
                    f"{base_url}/api/movements", headers=headers,
                    json={"item_id": random.choice(item_ids), "kind": "outbound", "quantity": 1, "recipient": "Load test dispatch", "note": "loadtest"},
                )
            else:
                resp = await client.post(
                    f"{base_url}/api/expenses", headers=headers,
                    json={"supplier": "Load Test Store", "quantity": 1, "amount": 12.5, "purpose": "Load test expense"},
                )
            elapsed = time.monotonic() - start
            async with lock:
                latencies.append(elapsed)
                if resp.status_code >= 500:
                    errors.append(resp.status_code)
        except Exception as exc:
            async with lock:
                errors.append(str(exc))
        await asyncio.sleep(random.uniform(0.05, 0.2))


async def run(base_url: str, duration: int, ids_path: Path) -> None:
    lines = ids_path.read_text().splitlines()
    user_ids = lines[0].split(",")
    item_ids = lines[1].split(",")

    latencies: list[float] = []
    errors: list = []
    lock = asyncio.Lock()
    stop_at = time.monotonic() + duration

    async with httpx.AsyncClient(timeout=10.0) as client:
        await asyncio.gather(*[one_user_loop(client, base_url, uid, item_ids, stop_at, latencies, errors, lock) for uid in user_ids])

    latencies.sort()
    n = len(latencies)

    def pct(p: float) -> float:
        return latencies[int(n * p)] * 1000 if n else float("nan")

    print(f"virtual users: {len(user_ids)}")
    print(f"duration: {duration}s")
    print(f"total requests: {n}")
    print(f"throughput: {n / duration:.1f} req/s")
    print(f"errors: {len(errors)}")
    if latencies:
        print(f"latency p50: {pct(0.50):.1f} ms")
        print(f"latency p95: {pct(0.95):.1f} ms")
        print(f"latency p99: {pct(0.99):.1f} ms")
        print(f"latency max: {latencies[-1] * 1000:.1f} ms")
    if errors:
        print("sample errors:", errors[:10])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--ids-file", default="loadtest/.ids.txt")
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.duration, Path(args.ids_file)))
