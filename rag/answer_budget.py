from threading import Lock
from time import monotonic


class BudgetExceeded(Exception):
    pass


class AnswerBudget:
    def __init__(self, deadline_ms: int, max_model_calls: int):
        self.started = monotonic()
        self.deadline = self.started + deadline_ms / 1000
        self.limit = max_model_calls
        self.calls: dict[str, int] = {"embedding": 0, "rerank": 0, "generation": 0}
        self.lock = Lock()

    def remaining(self) -> float:
        left = self.deadline - monotonic()
        if left <= 0:
            raise BudgetExceeded("deadline")
        return left

    def can_spend(self, calls: int) -> bool:
        return sum(self.calls.values()) + calls <= self.limit

    def model_timeout(self, kind: str) -> float:
        with self.lock:
            timeout = self.remaining()
            if not self.can_spend(1):
                raise BudgetExceeded("model_calls")
            self.calls[kind] += 1
            return min(20, timeout)
