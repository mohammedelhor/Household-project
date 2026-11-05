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
    uv run python -m data_engineering.synthetic_data --households --cancel-rate 0.1 --write
        # two households with different rhythms + cancelled mistaken purchases
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
from backend.app.db.event_admin import allow_event_correction
from backend.app.db.models import (
    Household,
    HouseholdMember,
    ItemEvent,
    Product,
    ShoppingList,
    ShoppingListItem,
    User,
)
from data_engineering.scopes import HOUSEHOLD, USER, Scope

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
    # Which household's list the item is on. None = the single legacy list of
    # the synthetic user (a PERSONAL list, so its scope is `user:<id>`).
    household: str | None = None
    # A phantom: bought by mistake, reverted, never bought again. It is NOT a
    # purchase and must not appear in the answer key.
    cancelled: bool = False

    @property
    def effective_purchase(self) -> datetime:
        """The purchase that counts: the LAST 'purchased' event.

        If someone tapped "purchased" by mistake, then "reverted", then
        "purchased" again, only the final tap is the real purchase. The
        pipeline we build later must learn this same rule.
        """
        if self.cancelled:
            raise ValueError("a cancelled item has no effective purchase")
        return max(e.occurred_at for e in self.events if e.event_type == "purchased")


def generate_history(
    profiles: tuple[ProductProfile, ...],
    *,
    end: datetime,
    days: int = 180,
    seed: int | str = 42,
    mistake_rate: float = 0.05,
    archive_rate: float = 0.8,
    cancel_rate: float = 0.0,
) -> list[PlannedItem]:
    """Create every item and event for `days` days ending at `end`.

    The same inputs always give the same output (that is what `seed` is for):
    a failing test must be reproducible, so nothing here uses real randomness.

    `cancel_rate` adds PHANTOM items: a "purchased" tap by mistake that is
    reverted and never repeated. They use a random stream of their own, so
    turning them on does not change a single real purchase (and the default
    0.0 leaves every earlier result identical).
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

    if cancel_rate > 0:
        items += _phantom_items(items, seed=seed, end=end, rate=cancel_rate)

    return items


def _phantom_items(real: list[PlannedItem], *, seed: int | str, end: datetime,
                   rate: float) -> list[PlannedItem]:
    """Mistaken purchases that were cancelled: purchased, reverted, never again."""
    phantoms: list[PlannedItem] = []
    for profile in sorted({i.profile for i in real}, key=lambda p: p.name):
        rng = random.Random(f"{seed}:{profile.name}:cancel")      # its own stream
        for item in (i for i in real if i.profile == profile):
            if rng.random() >= rate:
                continue
            tapped = item.effective_purchase + timedelta(days=rng.uniform(1, 4))
            reverted = tapped + timedelta(minutes=rng.uniform(1, 10))
            if reverted >= end:
                continue
            phantom = PlannedItem(profile=profile, created_at=tapped - timedelta(hours=rng.uniform(2, 24)),
                                  household=item.household, cancelled=True)
            phantom.events = [PlannedEvent("added", phantom.created_at),
                              PlannedEvent("purchased", tapped),
                              PlannedEvent("reverted", reverted)]
            phantoms.append(phantom)
    return phantoms


# ---------------------------------------------------------------------------
# 2b. Several households (the scenario that proves predictions are not mixed)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HouseholdSpec:
    key: str                              # stable id inside the generator
    name: str                             # stored as the household's name
    profiles: tuple[ProductProfile, ...]


# Same product, very different rhythm: Family A buys milk every 7 days, Family
# B every 30. If any layer merged their histories, the milk prediction of both
# would land in between and match neither.
TWO_HOUSEHOLDS: tuple[HouseholdSpec, ...] = (
    HouseholdSpec("family_a", "Synthetic Family A", (
        ProductProfile("Milk", "liter", Decimal("2"), 7, 1.0, "dairy"),
        ProductProfile("Eggs", "piece", Decimal("12"), 5, 1.0, "dairy"),
    )),
    HouseholdSpec("family_b", "Synthetic Family B", (
        ProductProfile("Milk", "liter", Decimal("2"), 30, 3.0, "dairy"),
        ProductProfile("Eggs", "piece", Decimal("12"), 12, 1.0, "dairy"),
        ProductProfile("Rice", "kg", Decimal("2"), 14, 1.0, "grains"),
    )),
)


def generate_households(
    specs: tuple[HouseholdSpec, ...],
    *,
    end: datetime,
    days: int = 180,
    seed: int = 42,
    mistake_rate: float = 0.05,
    archive_rate: float = 0.8,
    cancel_rate: float = 0.0,
) -> list[PlannedItem]:
    """One independent history per household (its own random streams)."""
    items: list[PlannedItem] = []
    for spec in specs:
        history = generate_history(
            spec.profiles, end=end, days=days, seed=f"{seed}:{spec.key}",
            mistake_rate=mistake_rate, archive_rate=archive_rate, cancel_rate=cancel_rate)
        for item in history:
            item.household = spec.key
        items += history
    return items


# ---------------------------------------------------------------------------
# 3. The answer key
# ---------------------------------------------------------------------------

def build_ground_truth(
    items: list[PlannedItem],
    *,
    end: datetime,
    days: int,
    seed: int,
    scope_keys: dict[str | None, str],
) -> dict:
    """Compute the exact numbers the pipeline must reproduce, PER SCOPE.

    `scope_keys` maps a household key (None = the legacy personal list) to the
    scope key the database gave it ("household:3", "user:7"). It is only known
    after the rows are written, so the caller passes it in; it is required on
    purpose, because guessing a scope would make the key meaningless.

    Intervals are measured between purchase DATES (whole days). Timestamps
    differ by a few hours, but "days between purchases" is the quantity the
    household actually cares about. Cancelled (phantom) items are not
    purchases and are left out.
    """
    scopes: dict[str, dict] = {}
    for household_key, scope_key in scope_keys.items():
        mine = [i for i in items if i.household == household_key and not i.cancelled]
        products = {}
        for profile in sorted({i.profile for i in mine}, key=lambda p: p.name):
            purchase_dates = sorted(
                i.effective_purchase.date() for i in mine if i.profile == profile
            )
            intervals = [(b - a).days for a, b in zip(purchase_dates, purchase_dates[1:])]
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
        scopes[scope_key] = {"household": household_key, "products": products}

    return {
        "note": "SYNTHETIC data. Answer key for validating the data engineering pipeline.",
        "seed": seed,
        "days": days,
        "end": end.isoformat(),
        "scopes": scopes,
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
    touch rows that belong to a synthetic user (or to a household whose
    members are all synthetic users).
    """
    user_ids = select(User.id).where(User.email.like(f"%@{SYNTHETIC_EMAIL_DOMAIN}"))
    household_ids = select(HouseholdMember.household_id).where(HouseholdMember.user_id.in_(user_ids))
    list_ids = select(ShoppingList.id).where(
        ShoppingList.owner_user_id.in_(user_ids) | ShoppingList.household_id.in_(household_ids)
    )

    # item_events is append-only in the database. Removing the events of a
    # SYNTHETIC user is the one legitimate deletion, so it says so explicitly
    # (valid for this transaction only; see backend/app/db/event_admin.py).
    allow_event_correction(session)
    session.execute(delete(ItemEvent).where(ItemEvent.list_id.in_(list_ids)))
    session.execute(delete(ShoppingListItem).where(ShoppingListItem.list_id.in_(list_ids)))
    session.execute(delete(ShoppingList).where(
        ShoppingList.owner_user_id.in_(user_ids) | ShoppingList.household_id.in_(household_ids)))
    # Keep the ids: the members must go before the households they point to.
    doomed = list(session.scalars(household_ids))
    session.execute(delete(HouseholdMember).where(HouseholdMember.user_id.in_(user_ids)))
    if doomed:
        session.execute(delete(Household).where(Household.id.in_(doomed)))
    session.execute(delete(User).where(User.email.like(f"%@{SYNTHETIC_EMAIL_DOMAIN}")))


def write_to_database(
    session: Session,
    items: list[PlannedItem],
    household_names: dict[str, str] | None = None,
) -> dict[str | None, str]:
    """Save everything. Returns {household key: scope key} (e.g.
    {None: "user:7"} or {"family_a": "household:3", ...}): the answer key
    needs the ids the database handed out.

    Items with household None go on ONE personal list of the synthetic user
    (the original behaviour). Every named household gets its own household,
    its own member (a synthetic user) and its own shared list.
    """
    clear_synthetic_rows(session)
    household_names = household_names or {}

    # Products are shared by every household: reuse one if it already exists
    # (the unique normalized_name would reject a second "rice").
    products: dict[str, Product] = {}
    for profile in sorted({i.profile for i in items}, key=lambda p: p.name):
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

    scope_keys: dict[str | None, str] = {}
    lists: dict[str | None, tuple[ShoppingList, User]] = {}
    for key in sorted({i.household for i in items}, key=lambda k: (k is not None, k or "")):
        first_activity = min(i.created_at for i in items if i.household == key)
        if key is None:
            user = User(email=SYNTHETIC_EMAIL, password_hash="synthetic-not-a-real-hash")
            session.add(user)
            session.flush()      # we need user.id for the list
            shopping_list = ShoppingList(
                owner_user_id=user.id, name="Synthetic household list",
                created_at=first_activity - timedelta(days=1))
            scope = Scope(USER, user.id)
        else:
            user = User(email=f"{key}@{SYNTHETIC_EMAIL_DOMAIN}",
                        password_hash="synthetic-not-a-real-hash")
            household = Household(name=household_names.get(key, key))
            session.add_all([user, household])
            session.flush()
            session.add(HouseholdMember(household_id=household.id, user_id=user.id, role="owner"))
            shopping_list = ShoppingList(
                household_id=household.id, name=f"{household.name} list",
                created_at=first_activity - timedelta(days=1))
            scope = Scope(HOUSEHOLD, household.id)
        session.add(shopping_list)
        session.flush()
        lists[key] = (shopping_list, user)
        scope_keys[key] = scope.key

    # Items first (events need their ids), oldest first.
    ordered = sorted(items, key=lambda i: i.created_at)
    rows: list[tuple[PlannedItem, ShoppingListItem]] = []
    for planned in ordered:
        shopping_list, user = lists[planned.household]
        row = ShoppingListItem(
            list_id=shopping_list.id,
            product_id=products[planned.profile.name].id,
            product_name=planned.profile.name,
            quantity=planned.profile.quantity,
            unit=planned.profile.unit,
            # A phantom was reverted: the item is back to "needed".
            status="needed" if planned.cancelled else "purchased",
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
                actor_user_id=lists[planned.household][1].id,
                occurred_at=occurred_at,
            )
        )

    session.commit()
    return scope_keys


# ---------------------------------------------------------------------------
# 6. Command line
# ---------------------------------------------------------------------------

def print_summary(truth: dict, items: list[PlannedItem]) -> None:
    event_count = sum(len(i.events) for i in items)
    phantoms = sum(1 for i in items if i.cancelled)
    print(f"\n{len(items)} items ({phantoms} cancelled), {event_count} events, "
          f"{len(truth['scopes'])} scope(s)\n")
    for scope_key, scope in truth["scopes"].items():
        print(f"[{scope_key}]")
        print(f"  {'product':<12}{'purchases':>10}{'configured':>12}{'realized':>10}")
        for name, p in scope["products"].items():
            print(
                f"  {name:<12}{p['purchases']:>10}{p['configured_mean_days']:>12}"
                f"{p['realized_mean_interval_days']:>10}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=180, help="history length")
    parser.add_argument("--seed", type=int, default=42, help="same seed = same data")
    parser.add_argument("--households", action="store_true",
                        help="two households that buy the same products at very different "
                             "rhythms (default: one personal list)")
    parser.add_argument("--cancel-rate", type=float, default=0.0,
                        help="share of purchases followed by a cancelled mistaken one")
    parser.add_argument("--write", action="store_true", help="actually write to the database")
    parser.add_argument("--truth-path", type=Path, default=DEFAULT_TRUTH_PATH)
    args = parser.parse_args()

    # override=False (the default): a variable already set in the shell wins.
    load_dotenv(PROJECT_ROOT / ".env")

    # "Now", cut to the start of today so two runs on the same day match.
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    if args.households:
        items = generate_households(TWO_HOUSEHOLDS, end=end, days=args.days, seed=args.seed,
                                    cancel_rate=args.cancel_rate)
        names = {spec.key: spec.name for spec in TWO_HOUSEHOLDS}
        planned_keys = {spec.key: f"household:?({spec.key})" for spec in TWO_HOUSEHOLDS}
    else:
        items = generate_history(PRODUCT_PROFILES, end=end, days=args.days, seed=args.seed,
                                 cancel_rate=args.cancel_rate)
        names = {}
        planned_keys = {None: "user:?"}
    # Before writing, the real ids do not exist yet: show a preview with placeholders.
    preview = build_ground_truth(items, end=end, days=args.days, seed=args.seed,
                                 scope_keys=planned_keys)
    print_summary(preview, items)

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
            scope_keys = write_to_database(session, items, names)
    finally:
        engine.dispose()

    truth = build_ground_truth(items, end=end, days=args.days, seed=args.seed,
                               scope_keys=scope_keys)
    args.truth_path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(f"Done. Answer key saved to {args.truth_path}")
    print(f"Scopes: {', '.join(scope_keys.values())}")


if __name__ == "__main__":
    main()
