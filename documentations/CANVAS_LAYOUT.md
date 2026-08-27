# CANVAS_LAYOUT.md
Where the boxes go — the shared layout behind both drawing canvases

Module: `app/services/canvas_layout/layout_service.py`, with
`app/schemas/canvas_layout/` for the request and the reply.
Consumed by two features and owned by neither, which is why it is its own.

---

# 1. What it is

One function. It takes ids and edges and says which **layer** and which **column** each
block belongs on, so a drawing reads top to bottom instead of wherever somebody last
dragged things.

```
POST /flow-builder/{flow_id:uuid}/layout      → {"positions": {...}, "back_edges": [...]}
POST /graph-designer/{graph_id:uuid}/layout   → the same
```

```python
layered_layout(nodes, edges, entry_ids) -> {
    "positions":  {node_id: {"layer": int, "column": float}},
    "back_edges": [index, ...],          # indices into the `edges` argument
}
```

Before this, neither canvas placed a block at all. The Flow Builder dropped each new one
at `x = 40 + (count % 6) * 40, y = 40 + floor(count / 6) * 160` and the Graph Designer did
the same with a wider stride — a diagonal stagger that wrapped back to the left margin
every sixth block. Every readable canvas in the product was one somebody had dragged into
shape by hand, and every unreadable one was a canvas they had not got round to.

---

# 2. Why the arithmetic is in Python

`app/services/tool_graphs/tool_graph_service.py` already made this call and wrote down the
reason: *layout is the part of a drawing that can be wrong without looking wrong, and only
the Python side of this repository has a test harness.* That is still literally true —
there is no JS test runner here, and even the chatbot widget script is asserted on by
Python tests reading the generated source.

So the algorithm is a service with `tests/unit/services/canvas_layout/` behind it, and the
browser does the half that needs a browser. The split is the one
`static/js/tool_graphs.js` states at the top of its own file.

**What the browser keeps.** Turning a column into an x, and stacking each layer below the
*measured* height of the tallest block above it. A Menu with four option pills is much
taller than a Send Message, and only the browser knows by how much — a layout that assumed
a uniform row height would either waste a band of empty canvas under every short row or let
a tall block overlap what is beneath it.

The same argument applies across the canvas, not only down it. A block is a fixed
`--fb-step-w` / `--gd-step-w` wide, but its **branch pills are not** — they sit side by
side and are allowed to carry their row wider than the block, which is the only way two
options of a Menu read as one choice. So `applyLayout` measures the widest pill row on the
canvas and makes *that*, not the block width, the distance between columns. A fractional
column — the mean of its parents — therefore still lands in a slot nothing else occupies,
and the first column starts far enough in that the left half of a wide row has somewhere to
be rather than being clipped by the edge. A canvas whose pills are all narrow gets the
plain block-width pitch back, so nothing is spread out for nothing.

**What it does not know.** Node *types*. It is handed ids and edges, which is why one copy
serves two canvases whose vocabularies have nothing in common. The single piece of type
knowledge either canvas needs — which block is the Start — is `ENTRY_NODE_TYPE` in
`app/schemas/canvas_layout/`, read off the posted nodes by the request schema rather than
trusted from the client, because a client naming its own entries could put the Start block
anywhere it liked.

---

# 3. The algorithm

Four passes, in this order. The order is the whole design: each one needs the last.

**1. Back edges, by depth-first walk.** An edge reaching a node that is *on the current
path* is the edge that closed a loop. Those are taken out and reported. Nothing else can
happen first: a graph with a cycle in it has no layers at all, and the Goto block in a real
flow means cycles are ordinary rather than exceptional.

Every node is walked, not only the ones the entries reach. A loop hanging off an
unreachable corner is still a loop, and an unwalked one would poison the layering.

**2. Layers, in one topological pass.** `layer[n] = max(layer[parent]) + 1` — the *longest*
path, not the shortest, which is what puts a block two branches of different lengths both
reach below the end of both. On shortest paths one of those connectors would run past a
whole row of blocks on its way down.

`tool_graph_service._layers` relaxes to a fixed point instead, and that is right there:
it is bounded to twenty nodes and the difference is not measurable. Here it is. With the
back edges gone the remainder is a DAG, so every node can be settled the moment its last
parent is — where relaxing a 500-block chain costs a pass per block, and the Graph Designer
puts no cap on how many nodes a pipeline may have. Measured: 4ms for 500 nodes, 16ms for
2,000.

**3. Components, undirected.** The drawing split into its disconnected parts, the part
holding the Start first, each laid out beside the last. A block added and not yet wired up
is its own part, and it needs to read as "not attached to anything" rather than as a step
of the chain beside it.

Connectivity is read **including** the back edges: a block joined to the chain only by a
Goto's return is part of that chain, and banding it separately would say it was stranded.

**4. Columns.** A pass down giving each block the mean column of its parents and shoving it
right if the block before it in the same row is already there — that alone draws a chain
straight and a fan-out fanned. Then centring passes, alternating "put a parent over the
middle of its children" with "put a child under the middle of its parents". That is what
closes a fan back up: six error branches converging on one End Flow leave it centred under
all six rather than under whichever one reached it first.

A block never moves past its neighbours in its own row, so no pass can make two overlap —
the bound is checked on every move rather than repaired afterwards.

**Columns are fractional on purpose.** A parent at 1.5 is centred over children at 1 and 2.

**Everything is deterministic.** Every iteration is over an input-ordered list or a sorted
key, never a dict's insertion order, and ties break on a node's position in the input. A
canvas that rearranged itself slightly on every reload would be worse than one that never
arranged itself at all — the rule `tool_graph_service._rows_of` states for its own rows.

---

# 4. Auto or manual, and how that is decided

`graph_data.layout` is `"auto"` or `"manual"`, stored with the drawing.

| | what happens |
| --- | --- |
| `auto` | The canvas arranges itself on open, and again after anything that changes the wiring. |
| `manual` | The stored positions are used as they are. |
| dragging a block | Switches to `manual`. Somebody who has placed a block has said where they want it. |
| dragging **several** blocks | The same, on the first frame that really moves. |
| **bending a connector** | The same. A hand-routed wire is only meaningful against a known arrangement. |
| **Tidy up** | Switches back to `auto` and re-arranges — and clears hand-routed bends, after asking. |
| no `layout` key | `auto` — which is every drawing saved before this existed, and is the point. |

Recorded rather than inferred, because inference here is a guess that is wrong for
somebody: a hand-arranged canvas and an auto-arranged one hold exactly the same kind of
coordinates.

**The switch happens on the first frame that moves, not on release.** It used to be written
at the end of the gesture, which left a window: the layout request is debounced, so its
answer can arrive while the mouse is still down, and `applyLayout` is free to re-place every
block for as long as the canvas is still `auto`. Behaviourally the two are identical — the
flag is monotonic — and the earlier one closes that window.

**Tidy up asks before clearing bends.** A connector routed by hand only means something
against a known arrangement, so leaving the bends across a freshly machine-placed drawing
gives something that is neither arranged nor hand-drawn, with no button that fixes it.
Clearing them is the consistent reading of a button that already means "throw away my
arrangement" — but it destroys work somebody did with their hands, so it asks, with the
count: *"Tidying up will straighten 3 connectors you routed by hand. Continue?"* See
[CANVAS_SELECTION.md](CANVAS_SELECTION.md).

**Consequence worth knowing.** An existing hand-arranged canvas *is* re-arranged the first
time it is opened. Nothing is written until Save, and Reload restores the stored drawing,
so it is recoverable — but it is a real change in behaviour rather than a no-op.

**The `position` fields keep being written.** The layout puts concrete `{x, y}` on every
node exactly as a drag does, so nothing else in the application — the engine, the
validators, the run view, the help pages — had to learn that layout exists.

**An auto-arrange does not mark the drawing unsaved.** A position changes nothing about
what a visitor experiences or what a run does, and the unsaved badge is a warning that the
behaviour on screen is not the behaviour that is live. Flagging every open of every flow
would make the badge mean nothing. Pressing Tidy up *does* mark it unsaved: that changes
what gets stored.

---

# 5. What the route does, and does not

**The drawing in the body is the input, never the stored one.** An operator arranges a
canvas that has unsaved changes, so laying out what the row holds would answer for a
picture one edit behind.

**Nothing is written.** The positions come back and are stored only if the operator then
presses Save.

**The row is still resolved**, so a drawing that is not this user's — or has been deleted
in another tab — is a 404 rather than a picture arranged for a graph that no longer exists.
That also keeps the endpoint behaving like every sibling on its controller.

**Only ids, types and ends are posted.** A block's settings are not the layout's business
and some of them are long — an AI Fallback's prompt, a SQL statement, an email body — and
this runs after every edit, debounced by 120ms.

**A failure is a note, never a blank canvas.** The blocks stay where they are and a warning
renders into the banner. The canvas may be holding unsaved work, and its arrangement is the
least important thing on it — the rule `static/js/integrations.js` states for its own
endpoints.

---

# 6. Drawing it: the orthogonal connectors

`static/js/graph_canvas.js` gained four functions beside the Bezier trio it already had:

```
elbowPoints(from, to, gutter)        down out of the source, across, down into the target
backEdgePoints(from, to, sideX)      down, out to a lane clear of the blocks, up, and in
elbowPathD(points, radius)           M/L/Q with the corners rounded
pointAlongPolyline(points, t)        where the ✕ and the two handles sit, by length
```

**Added beside, not instead of.** *Three* canvases share that module, not two:
`static/js/integrations.js` — the workflow canvas — still draws curves between boxes
standing side by side, and changing `geometry`/`pathD` in place would have silently
restyled a canvas nobody asked to change.

Two details in there are load-bearing:

* **The corner radius is clamped to half the shorter of the two segments meeting at a
  corner.** Without it, a corner near the end of a short segment overshoots into the next
  one and the line visibly doubles back — a kink that is very hard to read as a rounding
  bug.
* **An upward connector takes the return lane.** The obvious-looking alternative — step out
  below the source and climb in the *target's own column* — overshoots above the target and
  drops back into it, drawing a line folded over itself. A hand-dragged block can end up
  above the one feeding it, so this is reachable without a loop.

**`pointAlongPolyline` measures by length**, not by segment, so the ✕ on a connector whose
first leg is ten pixels and second is three hundred lands in the middle of the line a
reader sees.

**Since then it gained a fifth: `waypointRoute`, for a connector routed by hand.** Same
rule, added beside rather than folded into `elbowPoints` — and the property that makes it
safe is that with no waypoints it returns *exactly* `elbowPoints`, so `edgeRoute` on both
canvases calls it unconditionally and a drawing saved before bends existed is drawn
identically. Its own detail worth knowing: each turn goes on the axis the line **arrived**
on, alternating rather than fixed, because a fixed choice makes two consecutive segments
collinear and then doubled back — which `elbowPathD` can only draw as the kink described
above. And an explicit bend **overrides the return lane**: the lane is a fallback for having
nowhere down to go, and a person saying where a wire runs beats a fallback. See
[CANVAS_SELECTION.md](CANVAS_SELECTION.md).

---

# 7. What changed on the canvases themselves

Both were rebuilt around the same three ideas.

**A step, not a card.** A round coloured icon, the block's name under it, one line of its
settings under that. The colour is keyed by **what the block does** rather than one hue per
type, so two blocks that are the same kind of thing look it — Menu and Dropdown are one
question asked two ways. Every disc carries a white glyph at 3:1 contrast or better, which
is why none of them is a stock Bootstrap tint.

Two discs are keyed by *outcome* instead, and they are the same colour on both canvases:
**End Flow and the Graph Designer's Failure node are `#b02a37`** — where the thing stops —
and **the Success node is `#198754`**. End Flow used to share Goto's grey under one
`edge_of_flow` colour, which put "this conversation is over" and "carry on, elsewhere" in
one hue; Goto keeps the grey.

**Branch pills.** A block with one way out shows a dot on its bottom edge; a block with a
choice shows a labelled pill per way out. A Menu's options, a two-port block's
`written`/`failed`, a Branch node's conditions and an `on error` are all the same thing and
now look it. Each pill **is** the output port — it carries the `data-port` attribute the
connect, reattach and delete paths already keyed off, so none of those changed, and a
word-wide target replaced a 12px dot. On the Graph Designer it also replaced the label
drawn on the connector itself, which said the same word twice.

**A pill is coloured only when it is an outcome.** Green for the way out a block takes
when it did its work, red for the way out it takes when it could not — `written` / `failed`,
`queued` / `not sent`, `done` / `failed`. The test is narrow on purpose: a block has a green
pill only when it has **exactly two ways out and one of them is a failure**, which is what
makes the other one a success rather than merely the next step. A Menu's options, If / Else's
True / False, a Branch's conditions, a loop's `each` / `done` and a union's `next` / `execute`
all stay grey — a visitor pressing the second button has not made anything go wrong, and
green and red would say they had.

The connector leaving a failure port is drawn red **and dashed**, matching what the Graph
Designer already did with its `error` edges. Dashed as well as coloured, because a red line
and a grey line at 2px are the same line to a reader who cannot separate the two hues.

Pills sit **side by side**, wrapping only when the row reaches `--fb-branch-max` /
`--gd-branch-max`; a single pill is capped at `--fb-pill-max` / `--gd-pill-max` and
truncated with an ellipsis, with the whole label in its tooltip. Two things follow from
capping the pill rather than letting it run: one very long option cannot set the column
pitch for the entire canvas, and the common case — two options, or a `done` / `failed`
pair — fits on one line. Stacked pills were the shape the first version drew, and a
two-option Menu with sentence-length labels made a block three rows tall for what is one
question.

**Chrome on hover.** Every connector used to carry a red ✕ and two drag handles at all
times: ten connectors meant thirty controls competing with the blocks for attention, and
that was the single largest source of the clutter. They are the same controls, revealed when
a reader hovers that one line. Each connector also gained an invisible 16px-wide twin path
purely to be hovered — a 2px line is not a control anybody can hit.

The Flow Builder gained one thing it never had: **a Goto's jump is drawn**, dashed, round
the return lane to the block it names. Before this a flow that looped back to its own menu
looked like a flow that stopped.

**Since then, both canvases — and the Integrations one — gained a selection.** Drag a box on
empty canvas, Ctrl-click, or Ctrl+A, and then drag any selected block to move the whole set
with its connectors following. Two consequences belong on this page specifically:

* **The group's move is clamped as a group, not per block.** The single-block drag clamps
  each block to `x >= 0`, which is right for one and destructive for several: blocks at
  x = 10, 200 and 400 dragged left would leave the first at the wall while the rest kept
  going, permanently squashing the arrangement — and there is no undo on any of these
  canvases. The **delta** is clamped once against the leftmost member, so relative positions
  are exact for the whole gesture.
* **A connector can now be routed by hand**, which is the first thing on these canvases that
  the layout does not decide. It is stored as `waypoints` on the edge and it overrides the
  return lane; Tidy up clears it, after asking.

The drag loop was rewritten along with it, because a group move is exactly the workload it
was worst at: it wrote every position and then measured every port, forcing the browser to
lay the whole canvas out again a dozen times a frame. Ports are now measured once per gesture
and kept as offsets from their block's stored position. Full account in
[CANVAS_SELECTION.md](CANVAS_SELECTION.md).

---

# 8. Tests

`tests/unit/services/canvas_layout/test_layered_layout.py` asserts the properties a reader
of a canvas actually depends on: every connector points **down**; no two blocks share a
place; a loop is reported rather than layered; the same drawing lays out identically twice;
a block nobody has wired up still appears, and appears apart. Plus the mid-edit cases a live
canvas really posts — an edge to a just-deleted block, a node with no id, the same id
twice, a duplicated connector, a deleted Start.

`tests/unit/routes/canvas_layout/test_layout_routes.py` covers both endpoints in one module,
because it is one contract on two controllers and asserting it twice is how the two drift
apart. It includes `TestBothCanvasesAgree`, which posts one drawing at both and requires
byte-identical answers.

**Nothing about hand-routed bends reaches this service.** `requestLayout` posts only
`{id, type}` and `{source, target}`, so a `waypoints` key is invisible to `layered_layout`
and must stay that way — the arrangement is computed from the wiring, and a bend is not
wiring. The bends' own tests are in the schema layer; see
[CANVAS_SELECTION.md](CANVAS_SELECTION.md) §7, which also carries the manual checklist for
everything on these canvases that no Python test can reach.

**The JavaScript is not covered by the suite** — there is no runner for it, which is the
whole reason the algorithm is in Python. It was verified during development by driving both
canvases under jsdom against fixtures generated from real saved drawings: structure, pills,
connector shape, layer/column ordering, canvas growth, and the interactions (click a pill
then a step to connect, shift-click to pick, drag to switch to manual). That found two real
bugs — an unexported `ELBOW_LANE` that made every return lane `NaN`, and the folded-over
upward connector above.
