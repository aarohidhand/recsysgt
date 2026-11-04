"""Token-aware batching (D062, D064).

The endpoint's context (input + output) is 262,144 tokens, output <= 32,768; ``llm.max_tokens_per_call`` (240000) is the
enforced ceiling.  Runners pass ``llm.batch_completion_target`` (12000, <= max_completion_tokens) as the per-call output
bound, so batches are large but their expected reply stays reliable.  ``plan_batches`` packs items so that, per batch,

    estimated_input + max(estimated_completion, min_completion_tokens) <= budget
    estimated_completion <= max_completion_tokens        (runners: the batch_completion_target)
    len(batch) <= max_items                              (``llm.batch_max_constraints`` stays an upper bound)

It is pure and deterministic (greedy, order preserving): same inputs, same batches.  No network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, TypeVar, Union

T = TypeVar("T")
Cost = Union[int, Callable[[T], int]]


@dataclass(frozen=True)
class PlannedBatch:
    indices: tuple[int, ...]        # positions in the ``items`` list given to plan_batches
    est_input_tokens: int           # fixed prompt + items (what the client will estimate, to within overhead)
    est_completion_tokens: int      # expected reply size
    oversize: bool = False          # a single item that alone exceeds the budget (the client will refuse it)

    @property
    def size(self) -> int:
        return len(self.indices)

    def info(self, runner: str, batch_idx: int, n_batches: int) -> dict:
        """The per-request record logged in provenance (LLMClient event field ``batch``)."""
        return {"runner": runner, "batch_idx": batch_idx, "n_batches": n_batches, "batch_size": self.size,
                "est_input_tokens": self.est_input_tokens, "est_completion_tokens": self.est_completion_tokens,
                "oversize": self.oversize}


def _cost(c: Cost, item: T) -> int:
    return int(c(item) if callable(c) else c)


def plan_batches(
    items: Sequence[T],
    fixed_prompt_tokens: int,
    per_item_tokens: Cost,
    completion_per_item: Cost,
    budget: int,
    *,
    max_items: Optional[int] = None,
    fixed_completion_tokens: int = 0,
    max_completion_tokens: Optional[int] = None,
    min_completion_tokens: int = 0,
) -> list[PlannedBatch]:
    """Greedy, order-preserving packing of ``items`` into batches (see module docstring).

    ``per_item_tokens`` / ``completion_per_item``: an int or ``f(item) -> int`` (estimated tokens one item adds to the
    prompt / to the reply).  ``fixed_prompt_tokens``: everything in the request that does not depend on the batch
    (instructions, schema, shared variables, message overhead).  Returns ``[]`` for no items.
    """
    batches: list[PlannedBatch] = []
    cur: list[int] = []
    cur_in = fixed_prompt_tokens
    cur_out = fixed_completion_tokens

    def fits(inp: int, out: int, n: int) -> bool:
        if max_items is not None and n > max_items:
            return False
        if max_completion_tokens is not None and out > max_completion_tokens:
            return False
        return inp + max(out, min_completion_tokens) <= budget

    def flush(oversize: bool = False) -> None:
        nonlocal cur, cur_in, cur_out
        if cur:
            batches.append(PlannedBatch(tuple(cur), cur_in, cur_out, oversize))
        cur, cur_in, cur_out = [], fixed_prompt_tokens, fixed_completion_tokens

    for i, item in enumerate(items):
        di, do = _cost(per_item_tokens, item), _cost(completion_per_item, item)
        if cur and not fits(cur_in + di, cur_out + do, len(cur) + 1):
            flush()
        cur.append(i)
        cur_in += di
        cur_out += do
        if len(cur) == 1 and not fits(cur_in, cur_out, 1):
            flush(oversize=True)       # alone and still too big: its own batch, the client refuses it (typed error)
    flush()
    return batches
