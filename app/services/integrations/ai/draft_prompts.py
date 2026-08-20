"""
What a model is told when it drafts a workflow.

Kept apart from ``workflow_author`` for the reason ``prompt_builder`` is kept apart from
the Deep Agent service: a prompt is text that gets tuned, and text that gets tuned should
be diffable without a reader having to work out whether the surrounding logic changed too.

Three rules the prompt states, each of which exists because of a specific failure:

**Use only what is listed.** A model with no connection for Shopify and a request to sync
Shopify orders will otherwise emit a correctly-shaped step pointed at a URL it invented.
The catalogue is the constraint; this sentence is the reminder.

**Say so rather than guess.** ``unsupported`` and ``reason`` are given, and the prompt says
when to use them. A model with no way to decline produces a plausible workflow that answers
a different question, which is the worst available outcome — it looks finished.

**Steps in order, no wiring.** The model does not draw edges, does not choose ids and does
not place anything. It writes a list. Everything else is computed, because a model-drawn
batch whose body never returns is one batch of a hundred reported as a success, and the
drawing looks right.
"""

from typing import Sequence

from app.models.integrations import DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE
from app.schemas.integrations.workflow_draft_schemas import (
    MAX_DRAFT_ASSUMPTIONS,
    MAX_DRAFT_STEPS,
)

#: The step types a draft may use.
#:
#: **Deliberately a subset of what the engine implements**, and the three that are absent
#: are absent for one reason: each decides which records go where, and a generator cannot
#: make that decision from prose without guessing.
#:
#: ``validate`` and ``branch`` split a batch two ways, and where each half goes has
#: consequences — the invalid half either gets logged and dropped or takes a path somebody
#: drew. ``filter`` is subtler and worse: compiling "only EU orders" into an operator and a
#: typed value is exactly the guess that comes out meaning the opposite, and a filter that
#: silently keeps the wrong half is worse than no filter at all. ``validate_flow`` refuses a
#: filter with no conditions on the same grounds — one that lets every record through looks
#: like it is working — so a draft that emitted an unfinished filter could not be saved
#: anyway.
#:
#: All three are added by hand, on a canvas, by somebody who can see both edges.
DRAFTABLE_STEPS = (
    ("connector_read", "read records from a connection"),
    ("batch", "loop over the records a batch at a time"),
    ("transform", "map fields from the source record onto the destination's fields"),
    ("connector_write", "write records to a connection"),
)

DRAFTABLE_STEP_TYPES = frozenset(step for step, _ in DRAFTABLE_STEPS)


SYSTEM_PROMPT = """\
You design data-integration workflows. A workflow moves records from one system into \
another, a batch at a time.

You are given a catalogue of the connections this user actually has and what each one can \
do. **Use only what is in the catalogue.** Refer to a connection by the exact name shown \
in its heading. Never invent a connection, an operation or a field name — if what the user \
asked for needs something that is not listed, set `unsupported` to true and say what is \
missing in `reason`.

Describe the workflow as an ordered list of steps. Do not draw connections between them, \
do not choose ids, and do not position anything: the steps run in the order you list them \
and everything else is worked out afterwards.

Each step has:
- `ref` — a short handle of your choosing, unique within the draft, so a later step can \
refer to it
- `type` — one of the step types listed below
- `label` — what a person should see on the step
- `connection` — the connection's name, exactly as the catalogue spells it (read and write \
steps only)
- `operation` — the operation's id from the catalogue (read and write steps only)
- `source_ref` — the handle of an **earlier** step whose records this one uses; leave it \
out to use the step immediately before
- `mappings` — for a write step, which field of the record goes into which field of the \
destination
- `batch_size` — for a batch step

Step types:
{step_types}

Rules:
- A workflow that reads and then writes needs a `batch` step between them. Records travel \
a batch at a time; without one, the write step gets nothing.
- Every field marked `required` on a write step's operation should have a mapping. If you \
cannot tell which source field belongs in one, leave it out and say so in `assumptions` — \
do not guess a field name.
- A mapping's `target` must be a field the destination operation accepts, spelled exactly \
as the catalogue spells it.
- A mapping's `source` is a path into the record the previous step read, using dots for \
nesting: `customer.email`, `line_items[0].sku`.
- Do not set a schedule, and do not switch anything on. A person reviews this draft and \
decides.
- There is no step for filtering, validating or branching. If the request needs one, build \
the rest of the workflow and say so in `assumptions` — a person adds it on the canvas, \
where they can see where each side of the split goes.
- At most {max_steps} steps. Batch sizes between 1 and {max_batch} — {default_batch} is a \
sensible default.

List anything you had to decide for yourself in `assumptions`, at most \
{max_assumptions} of them. A guess you reported is one somebody can check; a guess you did \
not is indistinguishable from knowledge.
"""


def system_prompt() -> str:
    """The instructions, with the caps interpolated from where they are actually
    enforced — a prompt promising twelve steps against a schema allowing eight is a prompt
    that produces refusals nobody can explain."""
    steps = "\n".join(
        "- `" + name + "` — " + description for name, description in DRAFTABLE_STEPS
    )

    return SYSTEM_PROMPT.format(
        step_types=steps,
        max_steps=MAX_DRAFT_STEPS,
        max_batch=MAX_BATCH_SIZE,
        default_batch=DEFAULT_BATCH_SIZE,
        max_assumptions=MAX_DRAFT_ASSUMPTIONS,
    )


def user_content(instruction: str, catalogue_markdown: str) -> str:
    """
    The catalogue, then the request.

    **The request goes last.** A model reading a long catalogue and then a one-sentence
    request keeps the request in view; the other order pushes it behind everything it has
    to reason about, and a small local model answers the catalogue rather than the
    question.
    """
    return (
        catalogue_markdown
        + "\n\n## What to build\n\n"
        + str(instruction or "").strip()
    )


def repair_note(problems: Sequence[str]) -> str:
    """
    What is appended for the one retry.

    **The problems, not a re-explanation of the rules.** The rules are already in the
    system prompt, and repeating them uses the budget that the specific correction needs.
    Only resolvable faults reach here — a model that said ``unsupported`` is not asked
    again, because it has already answered.
    """
    listed = "\n".join("- " + problem for problem in problems)

    return (
        "\n\n## That draft could not be used\n\n"
        + listed
        + "\n\nProduce the draft again with those corrected. Use only names that appear in "
        "the catalogue above. If what was asked for cannot be built from it, set "
        "`unsupported` to true instead of guessing."
    )
