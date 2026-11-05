from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .serialization import epoch

StationId = Literal["kitchen", "bathroom", "printer"]
Severity = Literal["routine", "urgent", "critical"]


class Payload(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    item_id: str | None = Field(default=None, max_length=128)
    level: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


class DeviceEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    station_id: StationId
    type: Literal["button_pressed", "speech_transcript", "stock_low"]
    timestamp: str
    payload: Payload = Field(default_factory=Payload)

    @field_validator("timestamp")
    @classmethod
    def valid_timestamp(cls, value):
        epoch(value)
        return value

    @model_validator(mode="after")
    def required_payload(self):
        if self.type == "speech_transcript" and not self.payload.text:
            raise ValueError("Speech events require text")
        if self.type == "stock_low" and not self.payload.item_id:
            raise ValueError("Stock events require item_id")
        return self


class Transcript(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text", mode="before")
    @classmethod
    def trimmed(cls, value):
        return value.strip() if isinstance(value, str) else value


class Approval(BaseModel):
    proposal_fingerprint: str = Field(min_length=1)


class Assessment(BaseModel):
    severity: Severity
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    requires_confirmation: bool
    reason: str = Field(min_length=1, max_length=500)


class Decision(BaseModel):
    intent: Literal["purchase", "maintenance", "clarification"]
    item_id: str | None
    summary: str = Field(min_length=1, max_length=500)
    quantity: int = Field(ge=1, le=20, strict=True)
    question: str | None
    assessment: Assessment | None = None


class SensorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=128)
    station_id: StationId
    rule_id: str = Field(min_length=1, max_length=128)
    value: float = Field(ge=0, allow_inf_nan=False)
    timestamp: str

    @field_validator("timestamp")
    @classmethod
    def valid_timestamp(cls, value):
        epoch(value)
        return value


class SensorRule(BaseModel):
    id: str = Field(min_length=1)
    station_id: StationId
    metric: str = Field(min_length=1)
    direction: Literal["above", "below"]
    trigger_value: float = Field(ge=0, allow_inf_nan=False)
    reset_value: float = Field(ge=0, allow_inf_nan=False)
    min_samples: int = Field(ge=2, le=100)
    min_duration_seconds: int = Field(ge=1, le=3600)
    max_gap_seconds: int = Field(ge=1, le=300)
    item_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)
    severity: Severity = "urgent"

    @model_validator(mode="after")
    def hysteresis(self):
        if not (self.reset_value < self.trigger_value if self.direction == "above" else self.reset_value > self.trigger_value):
            raise ValueError("Reset threshold must provide hysteresis")
        return self
