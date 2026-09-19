"""Mechanical LLM Call Budget Enforcer.

Enforces the hard system constraint: Maximum ONE generative LLM call per query.
Any attempt to make a second generative call raises LLMBudgetExceeded.
"""

from dataclasses import dataclass


class LLMBudgetExceeded(Exception):
    """Raised when an execution node attempts to make more generative LLM calls than budgeted."""
    pass


@dataclass
class LLMCallBudget:
    max_calls: int = 1
    calls_made: int = 0

    def acquire(self) -> None:
        """Acquire permission to make a generative LLM call.

        Raises LLMBudgetExceeded if budget is exhausted.
        """
        if self.calls_made >= self.max_calls:
            raise LLMBudgetExceeded(
                f"LLMBudgetExceeded: attempted {self.calls_made + 1} calls, maximum budget is {self.max_calls}."
            )
        self.calls_made += 1

    def reset(self) -> None:
        self.calls_made = 0
