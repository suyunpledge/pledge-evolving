"""One ban table for every code-intake path.

Until now two independent safety scanners coexisted: registry scanned
contribution sources for forbidden imports/calls, while config ran its own AST
whitelist for row expressions. They agreed in spirit, diverged in detail, and
a change to one silently missed the other — the classic way such checks rot.

This module owns the shared ban surface. Both scanners import their tables
from here, so "add a ban" is a one-place change and the two scanners can never
drift apart again.
"""

from __future__ import annotations

import ast

# -- imports a contributed module may never pull in --------------------------
# It runs in-process with the framework: a network client or a recursive
# delete inside it is not a style issue.
FORBIDDEN_IMPORTS: frozenset[str] = frozenset({
    "socket", "http", "urllib", "requests", "ftplib", "smtplib", "telnetlib",
    "shutil", "subprocess", "ctypes", "multiprocessing", "pdb",
    # dynamic-import escape hatches (g3 F4-1): importlib re-arms every
    # forbidden root, builtins/__builtin__ hand out __import__ directly
    "importlib", "builtins", "__builtin__", "runpy", "zipimport",
    # data-driven execution (H6-M6): pickle.loads deserialises into code
    "pickle", "marshal", "shelve",
})

# -- dangerous imported symbols (checked on ImportFrom rows) -----------------
# ``from os import unlink as _u`` must not launder the name past the gate.
# H8 (DeepSeek rev.p0-2.6 receipt): every dotted FORBIDDEN_CALLS entry needs a
# matching FROM_NAMES twin, otherwise the non-aliased from-import l aunders it
# (``from operator import attrgetter`` used to MISS).
FORBIDDEN_FROM_NAMES: dict[str, frozenset[str]] = {
    "os": frozenset({"system", "popen", "unlink", "remove", "rmdir", "execv",
                     "execve", "execvp", "spawnv", "startfile", "kill",
                     # H9 (DeepSeek rev.p0-2.7 receipt): file-mutation
                     # primitives, same fence as remove/unlink/rmdir
                     "replace", "rename", "link", "symlink", "truncate",
                     "makedirs", "chmod", "utime",
                     # H10 (DeepSeek rev.p0-2.8 receipt): the four nearest
                     # siblings; os.* enumeration STOPS here -- chown/mknod/
                     # setxattr/fdopen etc. are explicitly registered to the
                     # runtime-hook batch so the static line stays closed
                     "mkdir", "removedirs", "open", "write"}),
    "os.path": frozenset(),
    "shutil": frozenset({"rmtree", "move", "unlink"}),
    "pathlib": frozenset({"Path"}),
    "io": frozenset({"open"}),
    "builtins": frozenset({"open", "eval", "exec", "compile", "__import__",
                           "getattr", "setattr", "delattr", "globals", "vars"}),
    "operator": frozenset({"attrgetter", "methodcaller"}),
    "subprocess": frozenset({"*"}),
    "ctypes": frozenset({"*"}),
    "socket": frozenset({"*"}),
}

# -- calls that must never appear in contributed source ---------------------
# bare-name forms: contrib code has no sanctioned reason to touch the filesystem
# or dynamic execution directly (reads/writes go through the api surface)
FORBIDDEN_CALLS: frozenset[str] = frozenset({
    "os.system", "os.popen", "os.unlink", "os.remove", "os.rmdir", "os.execv",
    "os.execve", "os.execvp", "os.spawnv", "os.startfile", "os.kill",
    # H9 (DeepSeek rev.p0-2.7 receipt): file-mutation primitives, same
    # fence as remove/unlink/rmdir -- mutations go through the api surface
    "os.replace", "os.rename", "os.link", "os.symlink", "os.truncate",
    "os.makedirs", "os.chmod", "os.utime",
    # H10 (DeepSeek rev.p0-2.8 receipt): nearest siblings, then STOP --
    # remaining os.* file primitives are runtime-batch material (see
    # FORBIDDEN_FROM_NAMES["os"] note); enumerating the namespace forever
    # would erode the closed status of the static line
    "os.mkdir", "os.removedirs", "os.open", "os.write",
    "shutil.rmtree", "shutil.move", "shutil.unlink",
    # H8 alignment: Path construction is step one of the unlink laundering
    # chain, so the constructor itself is a forbidden reference/call
    "pathlib.Path", "pathlib.Path.unlink",
    "io.open",
    "eval", "exec", "compile", "open", "input",
    # dynamic-dispatch escapes (g3 F4-1): getattr(obj, "system")(...) is a
    # jailbreak primitive, not a style issue; __import__ re-arms every root
    "getattr", "setattr", "delattr", "globals", "vars", "__import__",
    "importlib.import_module", "importlib.reload",
    # H7-N2 (DeepSeek rev.p0-2.5 receipt): attrgetter/methodcaller are
    # getattr by another name; operator itself stays importable
    "operator.attrgetter", "operator.methodcaller",
    # H8 alignment: builtins-module twins of the bare-name escapes
    # (``import builtins; builtins.eval(...)`` / alias-resolved forms)
    "builtins.open", "builtins.eval", "builtins.exec", "builtins.compile",
    "builtins.__import__", "builtins.getattr", "builtins.setattr",
    "builtins.delattr", "builtins.globals", "builtins.vars",
    # bare-name laundering tables (H6-M5): the interpreter injects
    # __builtins__ into every module; referencing it directly is a break-in
    "__builtins__", "__builtins",
})

# -- attribute names that must never be accessed on anything -----------------
# Dunder traversal (``x.__class__.__bases__[0].__subclasses__()``) and
# attribute tables (``os.__dict__["system"]``) are jailbreak primitives,
# not style issues (H6-M2/M4).
# Note: ``__class__`` is intentionally NOT here (DeepSeek rev.p0-2.5 advice
# option 2): ``o.__class__.__name__`` is idiomatic benign code, and escape
# chains stay blocked one link later at ``__bases__``/``__subclasses__``/
# ``__mro__``.
# -- dangerous leaf names (final segment of a chain) ------------------------
# Used only when a chain contains an index the tracker cannot resolve
# (``d[k].system``): we cannot prove the receiver is benign, so a leaf that is
# a known exec/fs sink is denied. Resolvable chains are matched exactly via
# FORBIDDEN_CALLS, so benign ``items.remove`` / ``s.replace`` stay clean.
DANGEROUS_LEAVES: frozenset[str] = frozenset({
    "system", "popen", "unlink", "remove", "rmdir", "removedirs", "mkdir",
    "makedirs", "execv", "execve", "execvp", "spawnv", "startfile", "kill",
    "replace", "rename", "link", "symlink", "truncate", "chmod", "utime",
    "rmtree", "move", "import_module", "reload", "loads", "load", "__import__",
})


FORBIDDEN_ATTRS: frozenset[str] = frozenset({
    # H7-N1: getattr's twin on the attribute path
    "__getattribute__",
    # H8 low-value candidates (DeepSeek rev.p0-2.6 §4): pickle protocol
    # entry and module-loader handles, same family as the dunder table
    "__reduce_ex__", "__loader__", "__spec__",
    "__bases__", "__subclasses__", "__mro__", "__globals__",
    "__code__", "__builtins__", "__builtins", "__dict__", "__closure__",
})

# -- AST nodes a config row expression may use ------------------------------
# Attribute access is refused outright: that is what blocks the usual
# ``().__class__.__bases__[0].__subclasses__()`` escape.
EXPRESSION_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Dict, ast.List, ast.Tuple,
    ast.Set, ast.Subscript, ast.Slice, ast.Call, ast.keyword, ast.BinOp, ast.UnaryOp,
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)

EXPRESSION_ALLOWED_NAMES: frozenset[str] = frozenset(
    {"ctx", "env", "true", "false", "null", "none"})

EXPRESSION_ALLOWED_CALLS: frozenset[str] = frozenset(
    {"get", "str", "int", "float", "bool", "len", "min", "max"})


def attr_name(node: ast.AST) -> str:
    """Render ``a.b.c`` from an Attribute/Name chain (shared helper).

    WB-P0 sibling: a walrus in the chain (``(z := os).system``) must not
    truncate to ``system`` -- the NamedExpr value is exactly what the target
    receives, so the chain sees straight through it.
    """
    parts: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        elif isinstance(node, ast.NamedExpr):
            node = node.value
        else:
            break
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


__all__ = [
    "EXPRESSION_ALLOWED_CALLS",
    "EXPRESSION_ALLOWED_NAMES",
    "EXPRESSION_ALLOWED_NODES",
    "DANGEROUS_LEAVES",
    "FORBIDDEN_ATTRS",
    "FORBIDDEN_CALLS",
    "FORBIDDEN_FROM_NAMES",
    "FORBIDDEN_IMPORTS",
    "attr_name",
]
