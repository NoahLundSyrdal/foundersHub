from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator, model_serializer


from .schemas import StationId


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Station(StrictModel):
    id: StationId
    name: str = Field(min_length=1, max_length=200)
    responsible_team: str = Field(max_length=200)
    equipment: list[str] = Field(max_length=30)
    item_ids: list[str] = Field(max_length=100)


class Product(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    station_id: StationId
    price_nok: float = Field(ge=0, allow_inf_nan=False)
    delivery_estimate: str = Field(max_length=200)
    supplier: str = Field(max_length=200)
    fulfillment_kind: Literal["goods", "service"] = "goods"


class Policy(StrictModel):
    max_order_nok: float = Field(gt=0, allow_inf_nan=False)
    require_approval: bool = True
    preferences: list[str] = Field(default_factory=list, max_length=30)


class OpenRequest(StrictModel):
    id: str = Field(max_length=128)
    station_id: StationId
    item_id: str | None
    status: str = Field(max_length=40)


class GroceryItem(StrictModel):
    query: str = Field(min_length=1, max_length=160)
    quantity: int = Field(ge=1, le=20, strict=True)

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A food search query is required")
        return value.strip()


class GroceryNeed(StrictModel):
    items: list[GroceryItem] = Field(max_length=10)
    summary: str = Field(max_length=500)
    question: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def items_or_question(self) -> "GroceryNeed":
        if not self.items and not (self.question and self.question.strip()):
            raise ValueError("Food items or a clarification question are required")
        return self


class WorkerInput(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    station: Station
    catalogue: list[Product] = Field(max_length=100)
    policy: Policy
    open_requests: list[OpenRequest] = Field(default_factory=list, max_length=500)
    grocery_enabled: bool = False
    provider: Literal["mock", "anthropic"] = "anthropic"
    model: str | None = Field(default=None, max_length=128)


class Assessment(StrictModel):
    severity: Literal["routine", "urgent", "critical"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    requires_confirmation: bool
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_review_for_critical_or_uncertain(self) -> "Assessment":
        if (self.severity == "critical" or self.confidence < 0.8) and not self.requires_confirmation:
            raise ValueError("Critical or low-confidence assessments require human confirmation")
        return self


class Interpretation(StrictModel):
    intent: Literal["purchase", "maintenance", "clarification", "grocery", "conversation"]
    item_id: str | None
    summary: str = Field(min_length=1, max_length=500)
    quantity: int = Field(ge=1, le=20, strict=True)
    question: str | None
    assessment: Assessment
    grocery: GroceryNeed | None = None


class Research(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    snippet: str = Field(max_length=1000)
    source: Literal["tavily", "anthropic_web_search"] = "tavily"
    executable: Literal[False] = False
    marketplace: Literal["finn"] | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_marketplace(self, handler):
        data = handler(self)
        if data.get("marketplace") is None:
            data.pop("marketplace", None)
        return data

    @field_validator("url")
    @classmethod
    def public_https(cls, value: HttpUrl) -> HttpUrl:
        # We do not fetch result URLs, but reject local/private links for clients too.
        import ipaddress
        host = value.host or ""
        if value.scheme != "https" or value.username or value.password:
            raise ValueError("Public HTTPS URLs only")
        if host.lower() == "localhost" or host.endswith((".local", ".localhost")):
            raise ValueError("Public HTTPS URLs only")
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            if "." not in host:
                raise ValueError("Public HTTPS URLs only")
        else:
            if not address.is_global:
                raise ValueError("Public HTTPS URLs only")
        return value


class Progress(StrictModel):
    stage: str = Field(max_length=60)
    detail: str = Field(max_length=300)


class WorkerOutput(StrictModel):
    interpretation: Interpretation
    research: list[Research] = Field(default_factory=list, max_length=10)
    progress: list[Progress] = Field(default_factory=list, max_length=20)
