"""Validated input to local operator work APIs; identities here never grant authority."""
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class WorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StepCreate(WorkInput):
    id: Key
    title: Text
    owner: Key
    depends_on: list[Key] = Field(default_factory=list, max_length=30)
    required_evidence: list[Key] = Field(default_factory=lambda: ["confirmation"], min_length=1, max_length=20)
    action_kind: Literal["human", "local", "purchase", "message", "booking", "access"] = "human"
    action_snapshot: dict = Field(default_factory=dict)
    requires_approval: bool = False

    @model_validator(mode="after")
    def unique_requirements(self):
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Step dependencies must be unique")
        if len(set(self.required_evidence)) != len(self.required_evidence):
            raise ValueError("Evidence requirements must be unique")
        return self


class WorkCreate(WorkInput):
    event_key: Key
    title: Text
    outcome: Text
    owner: Key = "office-coordinator"
    template_key: Key = "custom"
    context: dict[Key, Text] = Field(default_factory=dict)
    requester_id: Key | None = None
    priority: Literal["routine", "urgent", "critical"] = "routine"
    due_at: AwareDatetime | None = None
    next_review_at: AwareDatetime | None = None
    escalation_owner: Key = "office-coordinator"
    simulation: bool = True
    steps: list[StepCreate] | None = Field(default=None, min_length=1, max_length=30)

    @model_validator(mode="after")
    def valid_dependencies(self):
        if self.steps is None:
            if self.template_key == "custom":
                raise ValueError("Custom work requires steps")
            return self
        ids = {step.id for step in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("Step ids must be unique")
        edges = {step.id: step.depends_on for step in self.steps}
        visiting, visited = set(), set()
        def visit(key):
            if key in visiting:
                raise ValueError("Step dependencies contain a cycle")
            if key in visited:
                return
            if key not in edges:
                raise ValueError("Step dependency does not exist")
            visiting.add(key)
            for dependency in edges[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)
        for key in ids:
            visit(key)
        return self


class WorkAction(WorkInput):
    event_key: Key
    expected_version: int = Field(ge=1, strict=True)
    action: Literal["assign", "wait", "block", "resume", "cancel", "fail", "start_step", "complete_step", "simulate_step", "reconcile_step", "renew_approval", "close"]
    step_id: Key | None = None
    owner: Key | None = None
    reason: Text | None = None
    next_review_at: AwareDatetime | None = None
    reference: Text | None = None
    result: Literal["confirmed", "failed", "uncertain"] | None = None


class EvidenceCreate(WorkInput):
    event_key: Key
    expected_version: int = Field(ge=1, strict=True)
    step_id: Key
    kind: Key
    summary: Text
    source: Text
    recorded_by: Key


class WorkDecision(WorkInput):
    event_key: Key
    expected_version: int = Field(ge=1, strict=True)
    step_id: Key
    approve: bool
    proposal_fingerprint: Key
