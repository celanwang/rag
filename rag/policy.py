import hashlib
import re
from collections import defaultdict
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

PolicyKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
MARKER = "<!-- rag-policy "
SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？])\s*|(?<=[.!?])\s+|\n+")


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

    def matches_topic(self, question: str) -> bool:
        return any(label and label in question for label in self.topic_labels)


class PolicyAnchor(BaseModel):
    digest: str
    policy: Policy


def anchor_digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def match_policy_anchors(bodies: list[str], anchors: list[PolicyAnchor]) -> dict[int, Policy] | None:
    """Require each previously annotated passage to match one new chunk or complete sentence."""
    wanted = {anchor.digest for anchor in anchors}
    matches: dict[str, list[int]] = defaultdict(list)
    for index, body in enumerate(bodies):
        digests = {anchor_digest(part) for part in [body, *SENTENCE_BOUNDARY.split(body)] if part.strip()}
        for digest in wanted & digests:
            matches[digest].append(index)

    policies = {}
    for anchor in anchors:
        positions = matches[anchor.digest]
        if len(positions) != 1:
            return None
        index = positions[0]
        if index in policies and policies[index] != anchor.policy:
            return None
        policies[index] = anchor.policy
    return policies


POLICY_HEADERS = TypeAdapter(dict[str, Policy])
POLICY_ANCHORS = TypeAdapter(list[PolicyAnchor])


def read_metadata(text: str, markdown: bool) -> dict[str, Policy]:
    first = text.split("\n", 1)[0].rstrip("\r")
    if not markdown or not first.startswith(MARKER):
        return {}
    if not first.endswith(" -->"):
        raise ValueError("Unclosed policy metadata")
    return POLICY_HEADERS.validate_json(first[len(MARKER):-4])


def resolve_policy(heading: str, annotations: dict[str, Policy]) -> Policy | None:
    """Use the closest annotated section. An explicit child replaces the whole record."""
    while heading:
        if heading in annotations:
            return annotations[heading]
        heading = heading.rpartition(" / ")[0]
    return None


def section_policies(headings: set[str], annotations: dict[str, Policy]) -> dict[str, Policy]:
    for path in annotations:
        if not any(heading == path or heading.startswith(path + " / ") for heading in headings):
            raise ValueError(f"Policy annotation names a section with no evidence: {path}")
    return {heading: policy for heading in headings if (policy := resolve_policy(heading, annotations))}


def scope_hints(policies: list[Policy], question: str) -> list[str]:
    rules = {policy.rule for policy in policies if policy.matches_topic(question)}
    groups: dict[str, set[str]] = {}
    for policy in policies:
        if policy.rule in rules:
            groups.setdefault(policy.rule, set()).add(policy.scope_label)
    for labels in groups.values():
        if len(labels) > 1 and not any(label in question for label in labels):
            return sorted(labels)
    return []
