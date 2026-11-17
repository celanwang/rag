from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

PolicyKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
MARKER = "<!-- rag-policy "


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: PolicyKey
    rule: PolicyKey
    scope: PolicyKey
    scope_label: str = Field(min_length=1, max_length=60)
    topic_labels: list[str] = Field(default_factory=list, max_length=10)
    authority: int = Field(ge=1, le=10)
    value: str = Field(min_length=1, max_length=120)
    effective_from: date
    effective_to: date | None = None
    supersedes: list[PolicyKey] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_interval(self):
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        if self.id in self.supersedes:
            raise ValueError("A policy cannot supersede itself")
        return self

    def applies(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day < self.effective_to)


POLICY_HEADERS = TypeAdapter(dict[str, Policy])


def read_metadata(text: str, markdown: bool) -> dict[str, Policy]:
    first = text.split("\n", 1)[0].rstrip("\r")
    if not markdown or not first.startswith(MARKER):
        return {}
    if not first.endswith(" -->"):
        raise ValueError("Unclosed policy metadata")
    return POLICY_HEADERS.validate_json(first[len(MARKER):-4])


def scope_hints(policies: list[Policy], question: str) -> list[str]:
    groups: dict[str, set[str]] = {}
    for policy in policies:
        groups.setdefault(policy.rule, set()).add(policy.scope_label)
    for labels in groups.values():
        if len(labels) > 1 and not any(label in question for label in labels):
            return sorted(labels)
    return []
