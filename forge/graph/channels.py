"""State channels: per-key containers with reducer semantics.

Ported from langgraph/channels/* (upstream 5eb60ae). The channel is the
unit of state management: a node writes updates into channels, the
scheduler applies them through the channel's ``update()`` (reducer), and
checkpoints snapshot each channel's serialisable form.
"""
from __future__ import annotations

import operator
from collections.abc import Callable, Sequence
from typing import Any, get_args, get_origin

from .types import MISSING


class ChannelError(Exception):
    """Invalid channel update (e.g. two writers to a LastValue in one step)."""


class EmptyChannelError(ChannelError):
    """The channel was never written and has no value to read."""


class BaseChannel:
    """Contract every channel implements (subset of upstream BaseChannel)."""

    __slots__ = ("key", "typ")

    def __init__(self, typ: Any = Any, key: str = "") -> None:
        self.typ = typ
        self.key = key

    # -- persistence ---------------------------------------------------
    def checkpoint(self) -> Any:
        raise NotImplementedError

    def from_checkpoint(self, checkpoint: Any) -> "BaseChannel":
        raise NotImplementedError

    def copy(self) -> "BaseChannel":
        return self.from_checkpoint(self.checkpoint())

    # -- read ------------------------------------------------------------
    def get(self) -> Any:
        raise NotImplementedError

    def is_available(self) -> bool:
        try:
            self.get()
            return True
        except EmptyChannelError:
            return False

    # -- write -----------------------------------------------------------
    def update(self, values: Sequence[Any]) -> bool:
        raise NotImplementedError

    def consume(self) -> bool:
        return False

    def finish(self) -> bool:
        return False


class LastValue(BaseChannel):
    """Store the latest value; at most one writer per superstep.

    This is the default channel for un-annotated state keys, exactly as
    upstream: two nodes writing the same plain key in one step is the
    classic INVALID_CONCURRENT_GRAPH_UPDATE error.
    """

    __slots__ = ("value",)

    def __init__(self, typ: Any = Any, key: str = "") -> None:
        super().__init__(typ, key)
        self.value = MISSING

    def checkpoint(self) -> Any:
        return self.value

    def from_checkpoint(self, checkpoint: Any) -> "LastValue":
        ch = LastValue(self.typ, self.key)
        if checkpoint is not MISSING:
            ch.value = checkpoint
        return ch

    def get(self) -> Any:
        if self.value is MISSING:
            raise EmptyChannelError(self.key)
        return self.value

    def is_available(self) -> bool:
        return self.value is not MISSING

    def update(self, values: Sequence[Any]) -> bool:
        if not values:
            return False
        if len(values) > 1:
            raise ChannelError(
                f"key '{self.key}': can receive only one value per step; "
                "annotate the key with a reducer (e.g. Annotated[list, operator.add]) "
                "to merge concurrent writes"
            )
        self.value = values[-1]
        return True


class BinaryOperatorAggregate(BaseChannel):
    """Aggregate values through a binary reducer (operator.add etc.).

    The channel for ``Annotated[list[str], operator.add]`` keys: every
    write folds into the accumulated value, enabling parallel fan-in.
    """

    __slots__ = ("value", "operator")

    def __init__(
        self,
        typ: Any = Any,
        key: str = "",
        *,
        operator: Callable[[Any, Any], Any] | None = None,
    ) -> None:
        super().__init__(typ, key)
        self.operator: Callable[[Any, Any], Any] = operator or (lambda a, b: b)
        self.value: Any = MISSING

    def checkpoint(self) -> Any:
        return self.value

    def from_checkpoint(self, checkpoint: Any) -> "BinaryOperatorAggregate":
        ch = BinaryOperatorAggregate(self.typ, self.key, operator=self.operator)
        if checkpoint is not MISSING:
            ch.value = checkpoint
        return ch

    def get(self) -> Any:
        if self.value is MISSING:
            raise EmptyChannelError(self.key)
        return self.value

    def is_available(self) -> bool:
        return self.value is not MISSING

    def update(self, values: Sequence[Any]) -> bool:
        if not values:
            return False
        for v in values:
            if self.value is MISSING:
                self.value = v
            else:
                self.value = self.operator(self.value, v)
        return True


class EphemeralValue(BaseChannel):
    """A value visible for exactly one superstep, then consumed.

    Useful for fire-and-forget signals (e.g. 'retry requested'); the
    scheduler's ``consume()`` call clears it after the subscribing
    tasks in that step have read it.
    """

    __slots__ = ("value", "seen")

    def __init__(self, typ: Any = Any, key: str = "") -> None:
        super().__init__(typ, key)
        self.value = MISSING
        self.seen = False

    def checkpoint(self) -> Any:
        # Persist the pending trigger value: an interrupted run must still
        # see which branch channels fired. The ``seen`` flag is transient —
        # a restored trigger is re-available, and the runner's
        # ``versions_seen`` bookkeeping stops double-fires.
        return self.value

    def from_checkpoint(self, checkpoint: Any) -> "EphemeralValue":
        ch = EphemeralValue(self.typ, self.key)
        if checkpoint is not MISSING:
            ch.value = checkpoint
        return ch

    def get(self) -> Any:
        if self.value is MISSING:
            raise EmptyChannelError(self.key)
        return self.value

    def is_available(self) -> bool:
        return self.value is not MISSING and not self.seen

    def update(self, values: Sequence[Any]) -> bool:
        if not values:
            return False
        self.value = values[-1]
        self.seen = False
        return True

    def consume(self) -> bool:
        if self.value is not MISSING and not self.seen:
            self.seen = True
            return True
        return False


def channel_for_annotation(annotation: Any, key: str = "") -> BaseChannel:
    """Build the channel a state key declares.

    ``Annotated[T, reducer]`` with a binary callable → BinaryOperatorAggregate
    (upstream semantics); anything else → LastValue. ``get_type_hints``
    on TypedDicts resolves ``Annotated`` metadata, so user schemas work
    unchanged.
    """
    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        # Annotated[T, reducer, ...] → args = (T, reducer, ...)
        reducers = [a for a in args[1:] if callable(a) and not isinstance(a, type)]
        if reducers:
            return BinaryOperatorAggregate(args[0], key, operator=reducers[0])
    return LastValue(annotation, key)


__all__ = [
    "BaseChannel", "LastValue", "BinaryOperatorAggregate", "EphemeralValue",
    "ChannelError", "EmptyChannelError", "channel_for_annotation",
]
