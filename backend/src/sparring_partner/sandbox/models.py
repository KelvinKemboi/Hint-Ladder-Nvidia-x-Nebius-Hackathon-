from typing import Any

from pydantic import BaseModel


class ErrorInfo(BaseModel):
    type: str
    message: str
    line: int | None = None # line in the user's code, if the error came from there
    traceback: str = ""


class CallOutcome(BaseModel):
    """One function call in a batch run."""

    args: str # repr of the argument tuple
    output: str | None = None # repr of the return value
    error: ErrorInfo | None = None
    timed_out: bool = False
    elapsed_s: float = 0.0


class RunResult(BaseModel):
    backend: str
    exit_code: int | None = None
    timed_out: bool = False # the whole run hit its wall-clock limit
    stdout: str = "" # what the user's code printed
    stderr: str = ""
    error: ErrorInfo | None = None # exception while importing the code or running the harness
    value: Any = None # harness.run() return value: results, counterexamples, trace payloads
    outcomes: list[CallOutcome] = [] # batch mode
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.error is None
