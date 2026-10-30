"""Static facts about the user's code: which function or class to test, and its signature."""

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

    stateful = False

    def signature(self) -> str:
        params = ", ".join(
            f"{p.name}: {p.annotation}" if p.annotation else p.name for p in self.params
        )
        returns = f" -> {self.returns}" if self.returns else ""
        return f"{self.name}({params}){returns}"


@dataclass(frozen=True)
class ClassInfo:
    """A class is tested through its public methods, as a state machine.

    It is called like a function of one argument, `ops` (see
    testing.capture.stateful_target), so `params` is that one parameter.
    """

    name: str
    init: FunctionInfo  # __init__, without self
    methods: tuple[FunctionInfo, ...]  # public methods, without self
    docstring: str | None
    lineno: int

    stateful = True
    params = (Param("ops"),)

    def signature(self) -> str:
        lines = [f"class {self.name}:", f"    {self.init.signature()}"]
        lines += [f"    {m.signature()}" for m in self.methods]
        return "\n".join(lines)

    def arities(self) -> dict[str, int]:
        """Arguments per call, for the strategies: "__init__" and each method."""
        return {m.name: len(m.params) for m in (self.init, *self.methods)}


Target = FunctionInfo | ClassInfo


def _function_info(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, method: bool = False
) -> FunctionInfo:
    args = [*node.args.posonlyargs, *node.args.args][1 if method else 0 :]
    return FunctionInfo(
        name=node.name,
        params=tuple(
            Param(a.arg, ast.unparse(a.annotation) if a.annotation else None) for a in args
        ),
        returns=ast.unparse(node.returns) if node.returns else None,
        docstring=ast.get_docstring(node),
        lineno=node.lineno,
    )


def find_functions(code: str) -> list[FunctionInfo]:
    """Top-level functions in `code`. Raises SyntaxError for unparsable code."""
    return [
        _function_info(node)
        for node in ast.parse(code).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def find_classes(code: str) -> list[ClassInfo]:
    """Top-level classes with at least one public method. Raises SyntaxError."""
    classes = []
    for node in ast.parse(code).body:
        if not isinstance(node, ast.ClassDef):
            continue
        defs = [n for n in node.body if isinstance(n, ast.FunctionDef)]
        methods = tuple(
            _function_info(n, method=True)
            for n in defs
            if not n.name.startswith("_") and not n.decorator_list
        )
        init = next((n for n in defs if n.name == "__init__"), None)
        if methods:
            classes.append(
                ClassInfo(
                    name=node.name,
                    init=_function_info(init, method=True)
                    if init
                    else FunctionInfo("__init__", (), None, None, node.lineno),
                    methods=methods,
                    docstring=ast.get_docstring(node),
                    lineno=node.lineno,
                )
            )
    return classes


def defined_names(code: str) -> set[str]:
    """Names of the top-level functions and classes. Raises SyntaxError."""
    return {
        node.name
        for node in ast.parse(code).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def pick_function(code: str, intent: str, name: str | None = None) -> Target:
    """What to test: `name` if given, else the public function or class the intent fits best."""
    targets: list[Target] = [*find_functions(code), *find_classes(code)]
    if name is not None:
        for target in targets:
            if target.name == name:
                return target
        raise ValueError(f"no top-level function or class named {name!r}")

    candidates = [t for t in targets if not t.name.startswith("_")] or targets
    if not candidates:
        raise ValueError("no top-level function, or class with a public method, found")

    intent_words = set(re.findall(r"[a-z]+", intent.lower()))

    def score(target: Target) -> int:
        return len(set(_words(target.name)) & intent_words)

    # Ties go to the last definition: helpers tend to come before the main function.
    candidates.sort(key=lambda t: t.lineno)
    return max(reversed(candidates), key=score)


def _words(name: str) -> list[str]:
    """merge_intervals -> [merge, intervals]; LRUCache -> [lru, cache]."""
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", name)]
