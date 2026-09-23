from __future__ import annotations


class AgentError(Exception):
    """Base class for failures raised inside an agent.

    The subclass decides recovery: the engine never inspects error strings.
    """

    retryable = False


class TransientError(AgentError):
    """Provider timeout, rate limit, flaky tool. Same input may work next time."""

    retryable = True


class ContractError(AgentError):
    """Agent produced output that violates its declared schema or contract.

    Retryable once: a second attempt gets the validation message appended to its
    input, so the retry is informed rather than identical.
    """

    retryable = True


class DependencyError(AgentError):
    """A task ran without its declared blackboard inputs. A planning defect."""

    retryable = False


class GuardrailViolation(AgentError):
    """Agent attempted an action the policy layer forbids. Never retried."""

    retryable = False


class FatalError(AgentError):
    """Unrecoverable; the run should stop and escalate to a human."""

    retryable = False


class ApprovalDenied(Exception):
    """A human rejected a gated task."""


class ClarificationRequired(Exception):
    """Blocking ambiguity with no supplied assumption; the run halts by design."""

    def __init__(self, questions: list[str]):
        self.questions = questions
        super().__init__(f"{len(questions)} blocking ambiguities require a human answer")
