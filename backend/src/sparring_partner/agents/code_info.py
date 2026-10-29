"""Static facts about the user's code: which function to test and its signature."""

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    name: str
    annotation: str | None = None


@dataclass(frozen=True)
class FunctionInfo:
    name: str
    params: tuple[Param, ...]
    returns: str | None
    docstring: str | None
    lineno: int

    def signature(self) -> str:
        params = ", ".join(
            f"{p.name}: {p.annotation}" if p.annotation else p.name for p in self.params
        )
        returns = f" -> {self.returns}" if self.returns else ""
        return f"{self.name}({params}){returns}"


def find_functions(code: str) -> list[FunctionInfo]:
    """Top-level functions in `code`. Raises SyntaxError for unparsable code."""
    functions = []
    for node in ast.parse(code).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = [*node.args.posonlyargs, *node.args.args]
            functions.append(
                FunctionInfo(
                    name=node.name,
                    params=tuple(
                        Param(a.arg, ast.unparse(a.annotation) if a.annotation else None)
                        for a in args
                    ),
                    returns=ast.unparse(node.returns) if node.returns else None,
                    docstring=ast.get_docstring(node),
                    lineno=node.lineno,
                )
            )
    return functions


def pick_function(code: str, intent: str, name: str | None = None) -> FunctionInfo:
    """The function under test: `name` if given, else the public function the intent fits best."""
    functions = find_functions(code)
    if name is not None:
        for fn in functions:
            if fn.name == name:
                return fn
        raise ValueError(f"no top-level function named {name!r}")

    candidates = [fn for fn in functions if not fn.name.startswith("_")] or functions
    if not candidates:
        raise ValueError("no top-level function found; classes are not supported yet")

    intent_words = set(re.findall(r"[a-z]+", intent.lower()))

    def score(fn: FunctionInfo) -> int:
        return len(set(fn.name.lower().split("_")) & intent_words)

    # Ties go to the last definition: helpers tend to come before the main function.
    return max(reversed(candidates), key=score)
