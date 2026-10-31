"""Synthetic household purchase history (data engineering layer, step 1).

WHY THIS FILE EXISTS
--------------------
The data engineering layer will compute things like "how many days pass
between two purchases of rice?". To build and TEST that logic we need months
of purchase history, but the real database only has a handful of events.

So we generate a history where WE chose the answer in advance:
"rice is bought every 14 days, give or take a day". If the pipeline we build
later reports ~14 days for rice, the pipeline is correct. If it reports 9,
we know the bug is in the pipeline, not in the data. A test with a known
answer is much more useful than one where you only hope the number looks fine.

HOW IT IS ORGANISED (read it in this order)
-------------------------------------------
1. PRODUCT_PROFILES   - the "truth": what each product's rhythm is.
2. generate_history()  - PURE function: makes items and events in memory.
                         No database, so it is fast and easy to unit-test.
3. build_ground_truth()- the answer key computed from the generated data.
4. assert_safe_target()- refuses to run against the real or the test database.
5. write_to_database() - saves everything (only when --write is given).
6. main()              - the command line.

USAGE (from the project root)
-----------------------------
    uv run python -m data_engineering.synthetic_data           # dry run: prints only
    uv run python -m data_engineering.synthetic_data --write   # really writes
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from backend.app.core.units import SUPPORTED_UNITS
from backend.app.db.models import (
    ItemEvent,
    Product,
    ShoppingList,
    ShoppingListItem,
    User,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRUTH_PATH = Path(__file__).with_name("synthetic_ground_truth.json")

# ".invalid" is a top-level domain reserved so it can NEVER be a real address.
# Every synthetic row hangs off a user with this domain, which gives us a
# reliable way to find (and delete) our own data without touching anything else.
SYNTHETIC_EMAIL_DOMAIN = "synthetic.invalid"
SYNTHETIC_EMAIL = f"household@{SYNTHETIC_EMAIL_DOMAIN}"


# ---------------------------------------------------------------------------
# 1. The truth we choose
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProductProfile:
    """The purchase rhythm of one product."""

    name: str
    unit: str            # must be one of core/units.py SUPPORTED_UNITS
    quantity: Decimal    # typical amount bought each time
    mean_days: int       # buys again after about this many days...
    jitter_days: float   # ...give or take this (standard deviation)
    category: str


PRODUCT_PROFILES: tuple[ProductProfile, ...] = (
    ProductProfile("Rice", "kg", Decimal("2"), 14, 1.0, "grains"),
    ProductProfile("Milk", "liter", Decimal("2"), 5, 1.0, "dairy"),
    ProductProfile("Eggs", "piece", Decimal("12"), 7, 1.0, "dairy"),
    ProductProfile("Sugar", "kg", Decimal("1"), 21, 2.0, "pantry"),
    ProductProfile("Olive oil", "liter", Decimal("1"), 30, 3.0, "pantry"),
)


# ---------------------------------------------------------------------------
# 2. Generation (pure: no database)
# ---------------------------------------------------------------------------

@dataclass
class PlannedEvent:
    event_type: str          # added / purchased / reverted / archived
    occurred_at: datetime


@dataclass
class PlannedItem:
    """One shopping-list item and its full life, before it has database ids."""

    profile: ProductProfile
    created_at: datetime
    events: list[PlannedEvent] = field(default_factory=list)
    archived_at: datetime | None = None

    @property
    def effective_purchase(self) -> datetime:
        """The purchase that counts: the LAST 'purchased' event.

        If someone tapped "purchased" by mistake, then "reverted", then
        "purchased" again, only the final tap is the real purchase. The
        pipeline we build later must learn this same rule.
        """
        return max(e.occurred_at for e in self.events if e.event_type == "purchased")


def generate_history(
    profiles: tuple[ProductProfile, ...],
    *,
    end: datetime,
    days: int = 180,
    seed: int = 42,
    mistake_rate: float = 0.05,
    archive_rate: float = 0.8,
) -> list[PlannedItem]:
    """Create every item and event for `days` days ending at `end`.

    The same inputs always give the same output (that is what `seed` is for):
    a failing test must be reproducible, so nothing here uses real randomness.
    """
    if end.tzinfo is None:
        raise ValueError("`end` must be timezone-aware (UTC).")

    start_date = (end - timedelta(days=days)).date()
    items: list[PlannedItem] = []

    for profile in profiles:
        if profile.unit not in SUPPORTED_UNITS:
            raise ValueError(f"Unsupported unit in profile: {profile.unit}")

        # One random generator PER product, seeded with its name. Adding a
        # sixth product later will not change the rice history you already
        # validated against.
        rng = random.Random(f"{seed}:{profile.name}")

        # The first purchase lands at a random point inside the first cycle,
        # so all products do not start on the same day.
        day = start_date + timedelta(days=rng.randrange(profile.mean_days))

        while True:
            # Purchases happen on whole days, at a random time between 09:00
            # and 20:00 (so timestamps do not look machine-made).
            purchase_at = datetime.combine(day, time(0), tzinfo=timezone.utc) + timedelta(
                hours=rng.uniform(9, 20)
            )
            if purchase_at >= end:
                break

            # The need is noticed 2 hours to 2 days before the purchase.
            added_at = purchase_at - timedelta(hours=rng.uniform(2, 48))
            item = PlannedItem(profile=profile, created_at=added_at)
            item.events.append(PlannedEvent("added", added_at))
            item.events.append(PlannedEvent("purchased", purchase_at))

            # Sometimes a mistaken tap: purchased -> reverted -> purchased again.
            # Real data has these, so the pipeline must be tested against them.
            if rng.random() < mistake_rate:
                reverted_at = purchase_at + timedelta(minutes=rng.uniform(1, 10))
                again_at = reverted_at + timedelta(minutes=rng.uniform(1, 20))
                item.events.append(PlannedEvent("reverted", reverted_at))
                item.events.append(PlannedEvent("purchased", again_at))

            # Most bought items are cleaned off the list a day or two later.
            if rng.random() < archive_rate:
                archived_at = item.effective_purchase + timedelta(days=rng.uniform(1, 3))
                if archived_at < end:
                    item.archived_at = archived_at
                    item.events.append(PlannedEvent("archived", archived_at))

            items.append(item)

            # Next purchase: mean +/- jitter days, never less than 1 day.
            day += timedelta(days=max(1, round(rng.gauss(profile.mean_days, profile.jitter_days))))

    return items


# ---------------------------------------------------------------------------
# 3. The answer key
# ---------------------------------------------------------------------------

def build_ground_truth(
    items: list[PlannedItem],
    profiles: tuple[ProductProfile, ...],
    *,
    end: datetime,
    days: int,
    seed: int,
) -> dict:
    """Compute the exact numbers the future pipeline must reproduce.

    Intervals are measured between purchase DATES (whole days). Timestamps
    differ by a few hours, but "days between purchases" is the quantity the
    household actually cares about.
    """
    products = {}
    for profile in profiles:
        purchase_dates = sorted(
            i.effective_purchase.date() for i in items if i.profile is profile
        )
        intervals = [
            (b - a).days for a, b in zip(purchase_dates, purchase_dates[1:])
        ]
        products[profile.name] = {
            "unit": profile.unit,
            "configured_mean_days": profile.mean_days,
            "configured_jitter_days": profile.jitter_days,
            "purchases": len(purchase_dates),
            "intervals_days": intervals,
            "realized_mean_interval_days": (
                round(sum(intervals) / len(intervals), 4) if intervals else None
            ),
        }

    return {
        "note": "SYNTHETIC data. Answer key for validating the data engineering pipeline.",
        "seed": seed,
        "days": days,
        "end": end.isoformat(),
        "products": products,
    }


# ---------------------------------------------------------------------------
# 4. Safety guard
# ---------------------------------------------------------------------------

def assert_safe_target(synthetic_url: str | None) -> None:
    """Refuse to continue unless the target is a database dedicated to this.

    The generator deletes and inserts rows. If SYNTHETIC_DATABASE_URL were
    copied by mistake from DATABASE_URL (the real application) or from
    TEST_DATABASE_URL, fake data would mix with real data. Same idea as the
    guard in tests/conftest.py.
    """
    if not synthetic_url:
        sys.exit("SYNTHETIC_DATABASE_URL is not set. Add it to .env.")

    target = make_url(synthetic_url)

    for protected in ("DATABASE_URL", "TEST_DATABASE_URL"):
        other = os.getenv(protected)
        if not other:
            continue
        other_url = make_url(other)
        if (other_url.host, other_url.database) == (target.host, target.database):
            sys.exit(
                f"Refusing to run: SYNTHETIC_DATABASE_URL points to the same "
                f"database as {protected}."
            )


# ---------------------------------------------------------------------------
# 5. Writing to the database
# ---------------------------------------------------------------------------

def clear_synthetic_rows(session: Session) -> None:
    """Delete rows created by an earlier run, so re-running never duplicates.

    Order matters: children first, parents last (foreign keys). We only ever
    touch rows that belong to the synthetic user.
    """
    user_ids = select(User.id).where(User.email.like(f"%@{SYNTHETIC_EMAIL_DOMAIN}"))
    list_ids = select(ShoppingList.id).where(ShoppingList.owner_user_id.in_(user_ids))

    session.execute(delete(ItemEvent).where(ItemEvent.list_id.in_(list_ids)))
    session.execute(delete(ShoppingListItem).where(ShoppingListItem.list_id.in_(list_ids)))
    session.execute(delete(ShoppingList).where(ShoppingList.owner_user_id.in_(user_ids)))
    session.execute(delete(User).where(User.email.like(f"%@{SYNTHETIC_EMAIL_DOMAIN}")))


def write_to_database(session: Session, items: list[PlannedItem]) -> None:
    clear_synthetic_rows(session)

    user = User(email=SYNTHETIC_EMAIL, password_hash="synthetic-not-a-real-hash")
    session.add(user)
    session.flush()  # flush: we need user.id for the list

    first_activity = min(i.created_at for i in items)
    shopping_list = ShoppingList(
        owner_user_id=user.id,
        name="Synthetic household list",
        created_at=first_activity - timedelta(days=1),
    )
    session.add(shopping_list)
    session.flush()

    # Products: reuse one if it already exists (the unique normalized_name
    # would reject a second "rice"), otherwise create it.
    products: dict[str, Product] = {}
    for profile in {i.profile for i in items}:
        normalized = profile.name.strip().casefold()
        product = session.scalar(select(Product).where(Product.normalized_name == normalized))
        if product is None:
            product = Product(
                name=profile.name,
                normalized_name=normalized,
                default_unit=profile.unit,
                category=profile.category,
            )
            session.add(product)
            session.flush()
        products[profile.name] = product

    # Items first (events need their ids), oldest first.
    ordered = sorted(items, key=lambda i: i.created_at)
    rows: list[tuple[PlannedItem, ShoppingListItem]] = []
    for planned in ordered:
        row = ShoppingListItem(
            list_id=shopping_list.id,
            product_id=products[planned.profile.name].id,
            product_name=planned.profile.name,
            quantity=planned.profile.quantity,
            unit=planned.profile.unit,
            status="purchased",
            created_by_user_id=user.id,
            created_at=planned.created_at,
            archived_at=planned.archived_at,
        )
        session.add(row)
        rows.append((planned, row))
    session.flush()

    # Events go in CHRONOLOGICAL order across all items. Real ids grow with
    # time, and the extraction step will rely on "id of last event seen" to
    # fetch only new rows, so the synthetic ids must behave the same way.
    events = sorted(
        (
            (event.occurred_at, planned, row, event)
            for planned, row in rows
            for event in planned.events
        ),
        key=lambda t: t[0],
    )
    for occurred_at, planned, row, event in events:
        session.add(
            ItemEvent(
                item_id=row.id,
                product_id=row.product_id,
                list_id=row.list_id,
                event_type=event.event_type,
                quantity=row.quantity,
                unit=row.unit,
                actor_user_id=user.id,
                occurred_at=occurred_at,
            )
        )

    session.commit()


# ---------------------------------------------------------------------------
# 6. Command line
# ---------------------------------------------------------------------------

def print_summary(truth: dict, items: list[PlannedItem]) -> None:
    event_count = sum(len(i.events) for i in items)
    print(f"\n{len(items)} items, {event_count} events\n")
    print(f"{'product':<12}{'purchases':>10}{'configured':>12}{'realized':>10}")
    for name, p in truth["products"].items():
        print(
            f"{name:<12}{p['purchases']:>10}{p['configured_mean_days']:>12}"
            f"{p['realized_mean_interval_days']:>10}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=180, help="history length")
    parser.add_argument("--seed", type=int, default=42, help="same seed = same data")
    parser.add_argument("--write", action="store_true", help="actually write to the database")
    parser.add_argument("--truth-path", type=Path, default=DEFAULT_TRUTH_PATH)
    args = parser.parse_args()

    # override=False (the default): a variable already set in the shell wins.
    load_dotenv(PROJECT_ROOT / ".env")

    # "Now", cut to the start of today so two runs on the same day match.
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    items = generate_history(PRODUCT_PROFILES, end=end, days=args.days, seed=args.seed)
    truth = build_ground_truth(items, PRODUCT_PROFILES, end=end, days=args.days, seed=args.seed)
    print_summary(truth, items)

    if not args.write:
        print("\nDry run: nothing was written. Add --write to save to the database.")
        return

    url = os.getenv("SYNTHETIC_DATABASE_URL")
    assert_safe_target(url)
    target = make_url(url)
    print(f"\nWriting to host={target.host} database={target.database} ...")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            write_to_database(session, items)
    finally:
        engine.dispose()

    args.truth_path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(f"Done. Answer key saved to {args.truth_path}")


if __name__ == "__main__":
    main()