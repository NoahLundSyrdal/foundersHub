import json
import math
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")


def load(relative: str):
    return json.loads((ROOT / relative).read_text())


class Weights(BaseModel):
    price: float = Field(ge=0, allow_inf_nan=False)
    quality: float = Field(ge=0, allow_inf_nan=False)
    delivery: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def positive_sum(self):
        total = self.price + self.quality + self.delivery
        if not math.isfinite(total) or total <= 0:
            raise ValueError("Ranking weights require a finite positive sum")
        return self


class Policy(BaseModel):
    max_order_nok: float = Field(gt=0, allow_inf_nan=False)
    require_approval: bool
    preferences: list[str]
    service_requires_approval: bool = True
    auto_approval_min_confidence: float = Field(default=0.85, ge=0, le=1)
    auto_approval_max_nok: float = Field(default=300, ge=0, allow_inf_nan=False)
    ranking_weights: Weights = Field(default_factory=lambda: Weights(price=.4, quality=.35, delivery=.25))
    minimum_quality: float = Field(default=60, ge=0, le=100)
    max_delivery_hours: int = Field(default=168, ge=1, le=8760)
    quote_ttl_seconds: int = Field(default=900, ge=60, le=86400)


class Offer(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    station_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    name: str = Field(min_length=1, max_length=200)
    supplier: str = Field(min_length=1, max_length=200)
    unit_price_nok: float = Field(gt=0, allow_inf_nan=False)
    shipping_nok: float = Field(ge=0, allow_inf_nan=False)
    delivery_hours: int = Field(ge=0, le=8760)
    quality_score: float = Field(ge=0, le=100)
    quality_basis: str = Field(min_length=1, max_length=500)
    available: bool
    fulfillment_kind: str
    source: str
    simulation: bool

    @model_validator(mode="after")
    def simulation_only(self):
        if self.fulfillment_kind not in {"goods", "service"} or self.source != "fixture" or self.simulation is not True:
            raise ValueError("Only configured simulated offers are supported")
        return self


def configuration(overrides=None):
    office = load("config/office.json")
    office.update(overrides or {})
    stations = load("config/stations.json")
    products = load("fixtures/products.json")
    offers = [Offer.model_validate(o).model_dump() for o in load("fixtures/offers.json")]
    if any(o["station_id"] not in {s["id"] for s in stations} for o in offers):
        raise ValueError("Offers require a configured station")
    if len({o["id"] for o in offers}) != len(offers):
        raise ValueError("Offer IDs must be unique")
    return stations, products, offers, Policy.model_validate(office).model_dump()
