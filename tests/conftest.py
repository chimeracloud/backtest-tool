"""Shared fixtures for the test suite.

Provides duck-typed fakes that mirror the betfairlightweight resource
shapes consumed by the evaluator. Keeping these separate from the unit
tests means the same fixtures can be reused as the test suite grows.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class FakeRunner:
    selection_id: int
    last_price_traded: float
    status: str = "ACTIVE"

    @property
    def sp(self) -> "FakeSP | None":
        return getattr(self, "_sp", None)

    @sp.setter
    def sp(self, value: "FakeSP | None") -> None:
        self._sp = value


@dataclass
class FakeSP:
    actual_sp: float | None = None
    near_price: float | None = None
    far_price: float | None = None


@dataclass
class FakeMarketDefinitionRunner:
    selection_id: int
    name: str
    status: str = "ACTIVE"
    bsp: float | None = None


@dataclass
class FakeMarketDefinition:
    market_time: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    )
    venue: str | None = "Kempton"
    country_code: str | None = "GB"
    market_type: str | None = "WIN"
    in_play: bool = False
    runners: list[FakeMarketDefinitionRunner] = field(default_factory=list)


@dataclass
class FakeMarketBook:
    market_id: str = "1.234567890"
    runners: list[FakeRunner] = field(default_factory=list)
    market_definition: FakeMarketDefinition = field(
        default_factory=FakeMarketDefinition
    )
    inplay: bool = False
    publish_time: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, 13, 55, tzinfo=timezone.utc)
    )
    status: str = "OPEN"


def make_market(
    favourite_price: float,
    second_price: float | None = None,
    favourite_status: str = "ACTIVE",
    second_status: str = "ACTIVE",
    favourite_settled_status: str | None = None,
    second_settled_status: str | None = None,
    extra_runner_price: float | None = None,
    bsp_favourite: float | None = None,
    bsp_second: float | None = None,
    in_play: bool = False,
    country: str | None = "GB",
    market_type: str | None = "WIN",
) -> FakeMarketBook:
    """Construct a :class:`FakeMarketBook` with one to three runners.

    ``favourite_settled_status`` / ``second_settled_status`` override the
    runner status used by the settlement helper, so a single fixture can
    represent both the pre-off snapshot and the closed market state.
    """

    runners = [
        FakeRunner(selection_id=101, last_price_traded=favourite_price, status=favourite_status),
    ]
    if second_price is not None:
        runners.append(
            FakeRunner(
                selection_id=102, last_price_traded=second_price, status=second_status
            )
        )
    if extra_runner_price is not None:
        runners.append(
            FakeRunner(selection_id=103, last_price_traded=extra_runner_price)
        )

    if bsp_favourite is not None:
        runners[0].sp = FakeSP(actual_sp=bsp_favourite)
    if bsp_second is not None and len(runners) > 1:
        runners[1].sp = FakeSP(actual_sp=bsp_second)

    md_runners = [
        FakeMarketDefinitionRunner(
            selection_id=101,
            name="Alpha",
            status=favourite_settled_status or favourite_status,
            bsp=bsp_favourite,
        ),
    ]
    if second_price is not None:
        md_runners.append(
            FakeMarketDefinitionRunner(
                selection_id=102,
                name="Bravo",
                status=second_settled_status or second_status,
                bsp=bsp_second,
            )
        )
    if extra_runner_price is not None:
        md_runners.append(
            FakeMarketDefinitionRunner(selection_id=103, name="Charlie")
        )

    book = FakeMarketBook(
        runners=runners,
        market_definition=FakeMarketDefinition(
            country_code=country,
            market_type=market_type,
            runners=md_runners,
            in_play=in_play,
        ),
        inplay=in_play,
    )
    return book


@pytest.fixture
def make_book():
    """Pytest fixture returning the :func:`make_market` factory."""

    return make_market
