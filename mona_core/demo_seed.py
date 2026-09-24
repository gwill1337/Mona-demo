"""
demo_seed.py — one-off seeding of synthetic demo data (devices, metrics, anomalies).

Run once against the demo database (e.g. Neon), AFTER `alembic upgrade head`:

    DATABASE_URL="postgresql://...neon.tech/db?sslmode=require" \
        python -m mona_core.demo_seed

Safety:
  * Only touches devices/metrics/anomalies whose device name starts with "demo-".
    Anything else in the database is left alone.
  * Idempotent: re-running wipes the old demo-* data and generates it again.
  * Deterministic: the same --seed always produces the same curves.

Timestamps are naive UTC (the columns are TIMESTAMP WITHOUT TIME ZONE) and the
series ends at "now" (moment of seeding). If the API computes its windows relative
to the latest metric in the DB, the data never goes stale.
"""

import argparse
import math
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from mona_core.db import Anomaly, Base, Device, Metric, SessionLocal, engine

DEMO_PREFIX = "demo-"  # device names must be <= 15 chars (see validators.py)

# cpu/ram: (base, amplitude); period_min: length of the "daily wave" in minutes
PROFILES = [
    {
        "name": "demo-web-1",
        "ip": "10.0.0.11",
        "cpu": (24.0, 9.0),
        "ram": (46.0, 5.0),
        "noise": 2.0,
        "period_min": 180,
        "spikes": 3,
        "ram_spike_share": 0.3,
    },
    {
        "name": "demo-db-1",
        "ip": "10.0.0.12",
        "cpu": (35.0, 12.0),
        "ram": (68.0, 6.0),
        "noise": 2.5,
        "period_min": 240,
        "spikes": 3,
        "ram_spike_share": 0.7,
    },
    {
        "name": "demo-worker-1",
        "ip": "10.0.0.13",
        "cpu": (15.0, 6.0),
        "ram": (38.0, 4.0),
        "noise": 1.5,
        "period_min": 120,
        "spikes": 2,
        "ram_spike_share": 0.5,
    },
]


# ─── Helpers ────────────────────────────────────────────────────────────────


def _clamp(value: float, lo: float = 0.5, hi: float = 100.0) -> float:
    # lo > 0.1 because ml.py / tasks.py ignore rows with cpu <= 0.1
    return max(lo, min(hi, value))


def _describe_reason(cpu, ram, cpu_d1, ram_d1, cpu_d5, ram_d5) -> str:
    """Same rules as ml._describe_reason, copied so ml.py (sklearn) isn't needed."""
    parts = []

    if cpu > 85:
        parts.append(f"high cpu={cpu:.1f}%")
    if ram > 85:
        parts.append(f"high ram={ram:.1f}%")

    if abs(cpu_d1) > 20:
        sign = "▲" if cpu_d1 > 0 else "▼"
        parts.append(f"sudden cpu change {sign}{abs(cpu_d1):.1f}%")
    if abs(ram_d1) > 15:
        sign = "▲" if ram_d1 > 0 else "▼"
        parts.append(f"sudden ram change {sign}{abs(ram_d1):.1f}%")

    if abs(cpu_d5) > 30:
        parts.append(f"cpu changed by {cpu_d5:.1f}% in 25 sec")
    if abs(ram_d5) > 20:
        parts.append(f"ram changed by {ram_d5:.1f}% in 25 sec")

    return ", ".join(parts)


def _generate_series(profile: dict, n: int, step: int, rng: random.Random):
    """Returns (cpu, ram, spike_indices) — lists of length n."""
    cpu_base, cpu_amp = profile["cpu"]
    ram_base, ram_amp = profile["ram"]
    period_sec = profile["period_min"] * 60
    phase = rng.uniform(0, 2 * math.pi)

    cpu: list[float] = []
    ram: list[float] = []
    ram_level = ram_base

    for i in range(n):
        wave = math.sin(2 * math.pi * i * step / period_sec + phase)
        cpu.append(_clamp(cpu_base + cpu_amp * wave + rng.gauss(0, profile["noise"])))

        # RAM is smoother: mean-reverting random walk around a slow wave
        target = ram_base + ram_amp * wave * 0.5
        ram_level += (target - ram_level) * 0.03 + rng.gauss(0, 0.3)
        ram.append(_clamp(ram_level))

    # ── Spikes ──
    # One spike is always inside the last ~45 min, so the default "last hour"
    # dashboard view always contains an anomaly. The rest are spread over the history.
    recent_window = min(45 * 60 // step, n // 3)
    positions = [rng.randint(n - recent_window, n - 15)]

    older = profile["spikes"] - 1
    if older > 0:
        lo, hi = 10, n - recent_window - 20
        seg = max((hi - lo) // older, 1)
        for k in range(older):
            positions.append(rng.randint(lo + k * seg, lo + (k + 1) * seg - 1))

    spike_indices: list[int] = []
    for start in positions:
        length = rng.randint(4, 8)
        cpu_peak = rng.uniform(88, 98)
        with_ram = rng.random() < profile["ram_spike_share"]
        ram_peak = rng.uniform(87, 95)

        for j in range(length):
            k = start + j
            if k >= n:
                break
            cpu[k] = _clamp(cpu_peak + rng.gauss(0, 1.5))
            if with_ram:
                ram[k] = _clamp(ram_peak + rng.gauss(0, 1.0))
            spike_indices.append(k)
        # the point right after the spike is the sudden drop
        if start + length < n:
            spike_indices.append(start + length)

    return cpu, ram, sorted(set(spike_indices))


# ─── Seeding ────────────────────────────────────────────────────────────────


def seed(hours: float, step: int, seed_value: int, create_tables: bool) -> None:
    if create_tables:
        Base.metadata.create_all(engine)

    n = int(hours * 3600 // step)
    if n < 100:
        raise SystemExit("Too few points: increase --hours or decrease --step")

    rng = random.Random(seed_value)
    end = datetime.now(UTC).replace(tzinfo=None)  # naive UTC, like the DB columns
    start = end - timedelta(seconds=step * (n - 1))
    like = f"{DEMO_PREFIX}%"

    print(f"Target DB: {engine.url.render_as_string(hide_password=True)}")

    with SessionLocal() as db:
        try:
            # 1. wipe old demo data (only demo-*)
            deleted_a = db.execute(delete(Anomaly).where(Anomaly.device.like(like)))
            deleted_m = db.execute(delete(Metric).where(Metric.device.like(like)))
            print(
                f"Removed old demo data: {deleted_m.rowcount} metrics, "  # type: ignore[attr-defined]
                f"{deleted_a.rowcount} anomalies"  # type: ignore[attr-defined]
            )

            total_metrics = 0
            total_anomalies = 0

            for profile in PROFILES:
                name = profile["name"]
                assert name.startswith(DEMO_PREFIX) and len(name) <= 15

                # 2. device (upsert by name)
                device = db.execute(
                    select(Device).where(Device.name == name)
                ).scalar_one_or_none()
                if device is None:
                    db.add(Device(name=name, ip=profile["ip"], is_active=True))
                else:
                    device.ip = profile["ip"]
                    device.is_active = True

                # 3. metrics
                cpu, ram, spikes = _generate_series(profile, n, step, rng)
                metrics = [
                    Metric(
                        cpu=round(cpu[i], 2),
                        ram=round(ram[i], 2),
                        device=name,
                        timestamp=start + timedelta(seconds=i * step),
                    )
                    for i in range(n)
                ]
                db.add_all(metrics)
                db.flush()  # assigns metric ids for the anomalies below

                # 4. anomalies on spike points (same rules as ml.py)
                anomalies = []
                for i in spikes:
                    cpu_d1 = cpu[i] - cpu[i - 1] if i >= 1 else 0.0
                    ram_d1 = ram[i] - ram[i - 1] if i >= 1 else 0.0
                    cpu_d5 = cpu[i] - cpu[i - 5] if i >= 5 else 0.0
                    ram_d5 = ram[i] - ram[i - 5] if i >= 5 else 0.0

                    reason = _describe_reason(cpu[i], ram[i], cpu_d1, ram_d1, cpu_d5, ram_d5)
                    if not reason:
                        continue

                    # negative score, below SCORE_THRESHOLD (-0.05); worse = more negative
                    severity = max(cpu[i] - 85, 0) / 13
                    score = -round(min(0.45, 0.06 + severity * 0.15 + rng.uniform(0, 0.08)), 4)

                    m = metrics[i]
                    anomalies.append(
                        Anomaly(
                            metric_id=m.id,
                            cpu=m.cpu,
                            ram=m.ram,
                            timestamp=m.timestamp,
                            reason=reason,
                            score=score,
                            device=name,
                            detected_at=m.timestamp + timedelta(seconds=rng.randint(5, 55)),
                        )
                    )
                db.add_all(anomalies)

                total_metrics += len(metrics)
                total_anomalies += len(anomalies)
                print(f"  {name}: {len(metrics)} metrics, {len(anomalies)} anomalies")

            db.commit()
        except Exception:
            db.rollback()
            raise

    print(
        f"Done. {total_metrics} metrics, {total_anomalies} anomalies, "
        f"range {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic demo data (demo-* devices only)")
    parser.add_argument("--hours", type=float, default=12, help="history length (default 12)")
    parser.add_argument("--step", type=int, default=30, help="seconds between points (default 30)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default 42)")
    parser.add_argument(
        "--create-tables",
        action="store_true",
        help="run Base.metadata.create_all (for local tests; on Neon use alembic)",
    )
    args = parser.parse_args()
    seed(args.hours, args.step, args.seed, args.create_tables)


if __name__ == "__main__":
    main()
