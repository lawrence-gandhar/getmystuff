# Selecting and moving several things at once

Three pages in this application draw a graph you can rearrange: the **Graph Designer**, the
**Flow Builder**, and the **Integrations** workflow canvas. Until now each of them could move
exactly one box at a time, and each drew its connectors for you with no way to say the route
was wrong.

This page covers what changed, and — more usefully — why each decision went the way it did.

---

## 1. What you can do now

| You want to | Do this |
|---|---|
| Select several boxes | Drag a box on empty canvas. Anything it **touches** is selected |
| Add or remove one | **Ctrl-click** it (**Cmd-click** on a Mac) |
| Add to what is already selected with a box | Hold **Ctrl** while you start the box |
| Select everything | **Ctrl+A**, or the **Select all** button in the header |
| Clear the selection | **Escape**, click empty canvas, or press the button again — it reads **Clear (n)** |
| Move the selection | Drag any selected box. Everything selected moves together, connectors following |
| Move a box and the box it feeds | Select the **connector** between them and drag either end's box |
| Undo a move half way through | **Escape** while still dragging puts everything back |
| Route a connector by hand | Drag the line itself. It bends through the point you drop it at |
| Remove one bend | Drop it back onto the line the connector would take without it |
| Straighten a connector | **Double-click** it, or use **Straighten connector** in the Flow Builder's panel |
| Delete a connector | The red **✕** on its midpoint |
| Insert a block into a connector | The blue **+** beside the ✕. `A → B` becomes `A → new → B` |

Two things are deliberately **not** bound.

**Delete does not delete the selection.** None of these canvases has an undo, none of them
confirms a delete today, and some boxes cannot be deleted at all — the Graph Designer needs
exactly one Start and Integrations needs exactly one trigger, so a bulk delete would have to
report a partial success. A keystroke that irreversibly removes twelve boxes is a different
class of thing from one that changes a selection, and Backspace is "go back a page" in some
browser configurations. It is a feature with its own design, not a line in a key handler.

**Shift-click still means what it always meant.** In the Graph Designer, shift-click picks
nodes for a **test run** — that is why the move-selection uses Ctrl. Those are two genuinely
different questions about the same nodes ("which ones do I want to execute" and "which ones do
I want to drag"), they can both be non-empty at once, and the canvas draws them differently on
purpose. The header now carries two counts for that reason, and the button titles say which is
which.

---

## 2. Where the code lives, and why it is split that way

```
static/js/graph_canvas.js      stateless maths — shared, and was already shared
static/js/graph_selection.js   the gesture and the selection set — shared
static/css/graph_selection.css the rubber band — shared
static/js/graph_insert.js      the "+" menu on a connector — shared
static/css/graph_insert.css    that menu's appearance — shared
static/js/graph_edges.js       the connector runtime — shared by the two top-down canvases
static/js/graph_designer.js    what all of the above *means* here
static/js/flow_builder.js      what all of the above *means* here
static/js/integrations.js      the same, and its own connector runtime — see below
```

Four shared files, and the boundary between them is worth stating once, because "shared canvas
code" is not a useful distinction:

| | owns |
|---|---|
| `graph_canvas.js` | **maths**, and nothing stateful. Routes, rectangles, path strings |
| `graph_selection.js` | **a selection** — what is picked, and moving it as one |
| `graph_insert.js` | **a menu** — what a "+" offers, and where it appears |
| `graph_edges.js` | **connectors** — measuring anchors, repainting chrome, bending |

`graph_edges.js` is the only one of the four that is *not* shared with Integrations, and that
is a measurement rather than an oversight. Comparing the same-named functions across the three
canvases put the Flow Builder and the Graph Designer at 93–100% identical on this cluster —
`startBend` at 99%, `onBendEnd` at 100% — and Integrations at 19–40%. It draws a different
picture: steps side by side, Bézier curves, one control point rather than four waypoints.
Forcing it in would mean abstracting elbow-versus-curve behind a flag, which is how a shared
module becomes a worse copy of two working ones. It keeps its own runtime and shares only what
is genuinely common: `GC.waypointsOf`, `GC.readWaypoints`, and the selection's button painter.

### Every difference is a named config key

That was a requirement of the extraction, not a happy accident: it had to be behaviour-neutral
on two canvases already verified by hand. Where the two disagreed, neither won — the difference
became a config value:

| key | Flow Builder | Graph Designer |
|---|---|---|
| `chromePrefix` | `fb` | `gd` |
| `chromeYOffset` | `0` — the ✕ sits on the line | `10` — a node's label hangs under its disc |
| `isBusy()` | `reattaching \|\| dragging` | also `connecting` — it drags out of ports |
| `isRoutable(edge)` | excludes derived Goto jumps | everything is real |
| `sourceAnchor(edge)` | falls back to the node's own box | no fallback |
| `travelled(dx, dy)` | Chebyshev, `max(\|dx\|,\|dy\|)` | Manhattan, `\|dx\|+\|dy\|` |
| `getDrawableEdges()` | `drawableEdges()`, jumps included | omitted — `state.edges` |

The last-but-one is the interesting one: **the two canvases measure "has it moved yet"
differently**, and have since before this refactor. Preserved per canvas rather than unified,
because unifying it changes how one of them feels and this change was not allowed to do that.
Worth reconciling one day; not silently.

`graph_canvas.js` states at the top of the file that it holds nothing stateful, and it named
the selection model as one of the things that stays per-feature. That sentence is now wrong,
and the honest thing is to say what replaced it rather than delete it:

> **The geometry and the gesture are shared; what a selection means is not.**

A gesture *is* state — where the press began, what was selected before it, which animation
frame is pending — so it could not go in `graph_canvas.js` without breaking every promise that
file makes. It got its own module instead. The line moved; it did not blur.

What stayed per-canvas: which CSS class paints a selected thing, whether a properties panel
opens, what "commit" means (the two top-down canvases switch to manual layout; Integrations
has no layout to switch), and the single-box drag each file already had.

**That last one is why this was safe to add to three working canvases.** The shared module
takes a press only when it is a Ctrl-click or a genuine group move, and it says which by
returning a boolean:

```js
if (selection && selection.beginNodePress(node.id, e)) return;
startDrag(node.id, e);   // the existing path, unchanged
```

One line per canvas. Everything else — the Graph Designer's shift-click picking, the Flow
Builder's click-to-open-the-panel, the trailing-click suppression each already had — runs
exactly as before.

### `graph_selection.js`'s configuration surface

Eighteen keys, and the size is the honest cost of the abstraction rather than something to
hide. Every one of them is a place the three canvases genuinely differ:

- **DOM and naming** — the wrapper, the node layer, how an element id is built from a model id
  (`node-<id>` on two canvases, `int-node-<id>` on the third), which classes to paint.
- **Model access** — `getNodes()`, `getSelectableEdges()`, `edgeRoute(edge)`. The Flow Builder
  excludes its derived Goto jumps here: a jump is changed by editing the Goto block, so
  offering to select one would offer something that cannot be acted on.
- **Callbacks** — `onSelectionChange`, `onGroupMoveBegin`, `onGroupMoveFrame`,
  `onGroupMoveEnd`, `onEscape`, `onSwallowClick`.

**Pointer events, not mouse events**, and that is a decision about sharing rather than about
input. Two canvases drive their own drags with `mousedown` and the third with `pointerdown`.
Pointer events work from either starting point — for a mouse, `pointermove`/`pointerup` fire
whether the press was a `mousedown` or a `pointerdown` — so the module can be handed a
`MouseEvent` by one canvas and a `PointerEvent` by another without caring. The reverse is not
true, and choosing mouse events would have meant converting a working canvas to suit a new
module.

---

## 3. The decisions worth knowing about

### The box is a div, parented to the node layer

Not an SVG rect, and not a child of the scrolling wrapper.

- **Not SVG.** The connector layer is the obvious home, but it is emptied wholesale by
  `renderAllEdges()`, and it paints *under* the nodes — so the box would disappear behind every
  box it crossed.
- **Not the wrapper.** That works, but the wrapper has a 1px border, so the box's coordinate
  origin would be a pixel away from the origin `node.position` is measured against. A selection
  box that is one pixel out from the things it is selecting is a bug you find much later.
- **The node layer's padding box *is* the space node positions live in.** A `position:absolute`
  child of it, sized in px, lands pixel-exact with no arithmetic at all.

It is `pointer-events: none`, which is not decoration: `document.elementFromPoint` is how a
dropped connector finds the box under the cursor, and a rectangle painted over the canvas would
answer that question with itself.

### Touches, not contains

A box selects anything it overlaps. Containment would mean dragging a rectangle right around a
node to catch it — and a node's branch pills are wider than the node's own border box, so the
obvious gesture of sweeping down a column would catch nothing.

### The trailing click is the highest-risk bug in the whole feature

A click always follows the mouseup that ended a drag, and its target is whatever the press
landed on. For a box that is the wrapper — whose existing handler clears the selection. Without
suppression the box would select three boxes and the click behind it would immediately
unselect them, so **the feature would appear to do nothing at all**. The module arms a one-tick
flag on release and every canvas's click handlers consult it, using the same
`setTimeout(..., 0)` device the Graph Designer already had for its node clicks.

### The group clamp

The single-box drag clamps each box to `x >= 0`, which is right for one box and destructive for
several. Boxes at x = 10, 200 and 400 dragged left would leave the first stuck at the wall while
the other two kept going, and the shape of the selection would be permanently squashed — with
no undo on any of these canvases, that damage sticks.

So the **delta** is clamped, once, against the leftmost and topmost member of the group. Every
member gets the same delta, relative positions are exact for the whole gesture, and the group
slides along the wall instead of folding into it.

### The move mark appears when it means something

Pressing any box puts it in the move set — a drag of one box is a group of one, and the code
should not have two paths for that. But every canvas already draws a ring on the box whose
settings are open, so painting a second mark on top of it on every click would be saying
something you can already see. The dashed outline therefore appears once **more than one thing**
will move.

A connector is the exception and is marked whenever it is in the set: nothing else on the canvas
says a wire has been caught by a box, and without the mark, selecting one wire and dragging it
would move two boxes for no visible reason.

### Keyboard handling is bound to the canvas, not the document

The wrapper carries `tabindex="0"` and the key handler is on it. A document-level Ctrl+A would
have to *prove* the key was not meant for something else — every input in every properties
panel, every field in the mapping grid, the schedule form, the run dock's tables — and every
field added later would be a new chance to break typing in it. A handler on the wrapper simply
never fires when focus is elsewhere, and clicking anywhere on the canvas focuses it because the
browser focuses the nearest focusable ancestor of whatever was pressed.

The cost is that Ctrl+A does nothing until the canvas has been clicked once. That is what the
**Select all** button is for, and it takes focus when pressed so the keys work immediately
afterwards.

A `:focus-visible` outline was added with the `tabindex`: a focusable element with no visible
focus is an accessibility regression, and `:focus-visible` keeps the ring off a plain mouse
click.

### Escape is the one undo this feature can honestly offer

The start positions are already captured, so putting them back costs four lines. Escape during a
group move restores every box; Escape otherwise cancels a half-drawn connector first, then
clears the selection.

---

## 4. Bending a connector

A connector is routed for you, and now and then the route is wrong — it runs behind a box, or
two of them overlap into one line nobody can follow. Dragging the line puts a **bend** in it and
the line goes through that point from then on.

### The data

```json
{"id": "e1", "source": "n1", "target": "n2", "waypoints": [{"x": 340, "y": 260}]}
```

Canvas pixels, ordered source to target, at most **four** per connector on the two top-down
canvases and **one** on the Integrations canvas — where a connector is a single cubic curve with
a single control point, so one is not a chosen cap but what the geometry has.

Absent or empty must route **byte-identically** to before, and that is the compatibility
contract for every drawing already stored: `waypointRoute` with no waypoints returns exactly
`elbowPoints`, and `geometryWithBend` with no bend returns exactly `geometry`. Callers therefore
route everything through the new function and never branch.

**Absolute rather than relative to the two ends.** A bend exists to dodge something *on the
canvas* — another box, another wire — not to sit at a fixed offset from a port. A bend that
tracked its endpoints would slide off the thing it was put there to avoid. The cost of absolute
is staleness, and it is paid by two explicit rules rather than by a cleverer coordinate system:

- a group move carrying **both** ends of a wire carries its bends with it, from their captured
  start plus the same delta the boxes get;
- a move of only one end leaves the bends exactly where they are.

### The routing

`waypointRoute(from, to, waypoints, gutter)` leaves the source downward by the gutter, turns
once per waypoint, and comes into the target from above. Each turn goes on the axis the line
**arrived** on — across-then-down after arriving vertically, down-then-across after arriving
horizontally. Alternating rather than fixed, because a fixed choice makes two consecutive
segments collinear and then doubled back, which the path builder can only draw as a kink.

Near-duplicate points are collapsed, **and so are collinear triples** — without that, a bend
dragged into line with a stub leaves a corner that is not a corner, and the `×` ends up at a
slightly wrong place on a line that looks perfectly straight.

**An explicit bend overrides the return lane.** The lane is a fallback that exists because there
is no way down to a target that is above you; a waypoint is a person saying where the wire goes,
and a person's decision beats a fallback. The other order fails worse: you bend a wire, nothing
visible happens, and you bend it again.

**The layout response never touches a bend.** An edge can be reclassified as a back edge by the
server on any wiring change, and a background request silently deleting somebody's hand-routing
would be data loss. The wire may then draw an L that climbs, which is visible and fixable; a
deleted bend is neither.

### The gesture

The grab target is a fat invisible twin of the line, added to all three canvases here — none
of them had one before, and a 2px stroke is not something a hand can land on when the press
has to be held and dragged. It carries `cursor: grab`, because a wire that reads as clickable
gives no hint that it can also be pulled.

Press-and-move bends; press-and-release still selects — the same split the ports already used,
which is why the click handler stays and the bend arms a one-tick suppression on release.

`edgeGrabIntent(edgeId, e)` returns `"group"`, `"bend"` or `"ignore"`, and the one subtlety is
that `"group"` requires a **multi**-item selection rather than merely a selected connector.
Clicking a wire selects it, so if "selected" alone routed to a group drag, the ordinary sequence
of clicking a wire to read it and then dragging it would move two boxes instead of bending.

Grabbing within 8px of an existing bend moves that one; otherwise a new one is inserted at the
foot of the cursor on the leg that was grabbed, so the line does not jump the instant it is
taken hold of. At the cap, the nearest existing bend is moved instead and a sentence says why. A
press that never crosses the threshold removes the bend it inserted — clicking a wire must not
leave a bend behind.

A dragged bend snaps within 6px of either endpoint's x or y, or a neighbouring bend's, which is
what makes hand-routed wires line up rather than sit a pixel off.

### Clearing it

Three affordances, layered:

1. **Drop a bend back onto the line the wire would take without it** and it is spliced out. The
   wire straightens itself rather than keeping a bend that bends nothing.
2. **Double-click the wire** and every bend on it goes. This is the only option in the Graph
   Designer, which has no connector properties panel at all — which is why it is the gesture
   both canvases share.
3. **Straighten connector** in the Flow Builder's connector panel, shown only when there is
   something to straighten.

### Tidy up asks first

Tidy up already means "throw away my arrangement and let the canvas decide". A hand-routed wire
is only meaningful against a known arrangement, so leaving the bends would produce a drawing
that is neither arranged nor hand-drawn, with no button that fixes it. So Tidy up clears them —
but it destroys work done by hand, so it asks, with the count:

> Tidying up will straighten 3 connectors you routed by hand. Continue?

A bend also switches the canvas to **manual**, for the same reason moving a box does: an
auto-arrange is free to move both of a wire's ends out from under it.

---

## 5. The "+" — inserting a block into a connector

Every connector carries two controls on its midpoint: the red **✕** that deletes it, and a
blue **+** that puts a block *inside* it. `A → B` becomes `A → new → B` — the original
connector is replaced by two, so the block arrives already wired.

The alternative was three gestures: add the block from the palette, wire it up twice, and
remember to delete the connector you have just bypassed. Forgetting the last one leaves a
drawing where two paths leave the same port, which the save then refuses.

### The menu is shared; what may be inserted is not

`static/js/graph_insert.js` owns the menu: where it opens, how it is dismissed, arrow-key
navigation, and what an empty catalogue says. It owns nothing about blocks. The three canvases
genuinely disagree about those — a Flow Builder block's ports come from a local registry, a
Graph Designer node's from a server vocabulary, an Integrations step's from a spec whose ports
are bare strings — so each canvas supplies its own list and does its own splice. That is the
same line `graph_selection.js` draws, for the same reason.

The menu is parented to `document.body` rather than to the canvas, and that is load-bearing
rather than tidy: both the wrapper and the canvas scroll, so a menu inside either is clipped
by whichever edge it opens near — which is exactly where a connector in a long pipeline sits.

### Which port carries the connection onward

This is the one decision in the feature that is easy to get quietly wrong. `A → B` becomes
`A → new → B`, and `new` may have several ways out. Taking the first is the obvious rule:

```
for_each  →  ["body", "done"]
```

`body` is the *inside* of the loop. Wiring `B` there does not insert a step before `B`, it
moves `B` into the loop and runs it once per item — a silent change to what `B` means, on a
canvas with no undo. `done` is the port that means what the original connector meant.

So `GC.continuationPort(names)` decides, and it is shared because all three canvases need the
same answer: an explicit `default`, else `done`, else whatever is first. The last case is a
fresh Branch, whose only port is `else`, which is genuinely where a branch with no conditions
yet sends everything.

### What is not offered, and why each exclusion is correctness

A type with **no way out** — End Flow, Goto — cannot carry the connection onward. Splicing one
does not add a step, it severs the rest of the drawing, and the first clue is a run that stops
early or a conversation that dies. A **Start** or **trigger** is excluded from the other side:
nothing may lead into it, and there is only ever one.

**Menu and Dropdown** look like an oversight and are not. Their ports *are* their options, so a
freshly added one has none and there is no port for the flow to continue through. They stay in
the main palette, where you add one, give it options, and wire it deliberately.

The Graph Designer filters **per connector** rather than once, which is why `getChoices`
receives the edge id: **Success** and **Failure** both have a `then` port, so ports alone do
not rule them out, but an outcome node after another outcome node decides nothing. That rule
already exists as `OUTCOME_CHAIN_REFUSAL` for the two gestures that can draw such an edge;
offering it here and refusing it a moment later would have been a third copy of it.

### Two smaller decisions

**The splice is a filter plus two pushes**, never a mutation plus one push, so a half-applied
splice cannot exist: either both new connectors are there or the original still is.

**The `+` stops `mousedown`/`pointerdown` from reaching the wire underneath.** Without that the
press starts a bend, and opening the menu would put a corner in the line as a side effect.

## 6. The drag was slow, and why

This shipped first, on its own, because it is worth having with no new UI attached and because a
group move is exactly the workload the old drag loop was worst at.

**The read-after-write.** Every frame wrote `style.left` on the box and then called
`getBoundingClientRect()` on port elements to find where the connectors should attach. A rect
read after a style write forces the browser to lay the whole canvas out again — once per port,
per connector, per mousemove — and mousemove fires faster than the screen repaints. Dragging one
box with six connectors did that a dozen times a frame.

A drag now measures each port once and keeps the result as an **offset from its box's stored
position**, so an anchor becomes two additions and the loop reads nothing. Taking the offset as
`anchor - node.position`, both sampled at the same instant, is what makes it exact: two constant
discrepancies live between those two spaces — the wrapper's 1px border, and the ports' half-pixel
CSS placement — and both land inside the offset, where neither has to be named nor maintained if
the stylesheet changes.

**One frame, not one event.** A mousemove now records where the cursor is and asks for an
animation frame; one frame does the drawing, however many events arrived in between.

**Work hoisted out of the loop.** The affected-connector list and each connector's DOM elements
are resolved once at the start of a drag — the Flow Builder was rebuilding `drawableEdges()`
every frame, which rescans every box for Goto jumps, and both canvases were re-running six
`querySelector` calls per connector to find the same elements again. `returnLaneX()` is memoised
per frame, guarded on the anchor cache so it cannot outlive the gesture that justified it.

**The Graph Designer's connector repaint was destroy-and-rebuild.** `redrawEdgesForNode` removed
the whole `<g>` and called `renderEdge` again — two paths, the `×` with its two children, both
end handles and all five of their listeners, every frame, per connector. That is why a dragged
line flickered: the group was briefly not in the document at all. It now updates four attributes
in place, the way the Flow Builder already did.

**The Integrations canvas was the worst of the three**: its drag called `renderEdges()`, which
rebuilt *every* connector on the canvas — path, delete circle, `×`, port label — on every
pointermove. Its connectors are now grouped per edge with an in-place updater, and its drag
state moved out of closures into the module so a frame can find it.

### Four bugs found in those same functions

- **The Graph Designer wrote `node.position` before consulting the drag threshold.** A
  one-pixel click on a node quietly changed its stored position: discarded on the next
  auto-arrange, or kept as an unmarked edit in manual mode.
- **The Flow Builder dereferenced the node element with no null check**, where the Graph
  Designer had always guarded. A box deleted mid-drag threw.
- **The Flow Builder only built a connector's hit path and chrome when a route could be
  measured**, and the in-place updater could never add them later — so a connector rendered
  before both its ends were in the DOM could not be selected, deleted, reattached or bent for
  the rest of the session.
- **`renderEdge` appends**, and every existing caller reached it through `renderAllEdges()`,
  which had just emptied the group. Rebuilding a single connector — which the bend gesture needs
  to do — left two elements sharing one id, with `getElementById` finding the stale one. Both
  canvases now have one `reRenderEdge` that removes first.

`state.layout` also now switches to manual on the **first frame that moves** rather than on
release. Behaviourally identical, and it closes the window where the debounced layout response
lands mid-drag and re-places every box under the cursor.

---

## 7. What the server had to do

Almost nothing, and that is by design: every canvas save schema deliberately allows keys it does
not declare, because the drawing's shape belongs to the client. `waypoints` rides on that the
same way `layout` already does — no route, service, model or migration changed.

**One validator was added**, in `app/schemas/base.py` because all three canvases need exactly
the same two numbers and a cap that differed between them would be wrong on two:

```python
MAX_EDGE_WAYPOINTS = 4
MAX_CANVAS_COORD = 200_000
validate_edge_waypoints(edges, max_waypoints=MAX_EDGE_WAYPOINTS)
```

It exists for one reason above the others. `{"waypoints": [{"x": NaN, "y": 0}]}` satisfies every
other rule in the layer; `json.dumps` then writes a bare `NaN`, which **PostgreSQL's `jsonb`
rejects** — so the save dies as a 500 with a stack trace and no sentence, which is precisely
what this project's error contract exists to prevent. `Infinity` behaves the same. Neither is
reachable from the canvas; both are reachable from a hand-made request.

The other rules are ordinary: a list, at most four entries, each an object with two finite
numbers inside the canvas. Every refusal is a sentence a reader can act on.

Anything without the key passes through untouched — the edge vocabulary is still the service's to
interpret.

**One thing deliberately not done.** `GraphSaveRequest` has no cap on its node and edge counts,
unlike its Flow Builder twin, and adding one looked like an obvious fix. It is not: the file has
a written decision and a test arguing for it — a thousand-node data pipeline is not a runaway
client, and what bounds a *run* there is the per-loop iteration ceiling. The bend cap is per
connector, so it bounds the new field proportionally without reversing that.

---

## 8. Tests

**There is no JavaScript test *runner* in this repository** — no `package.json`, no Jest, no
Vitest — so most of this feature cannot be unit tested from its own tooling, and saying so is
more useful than implying otherwise. What exists instead, for the part of this feature that can
be reached at all, is `tests/unit/static_js/`: real `pytest` tests that shell out to `node`,
exactly the way `tests/unit/services/chatbot/test_widget_markdown.py` already runs the widget's
Markdown renderer. Skipped, not failed, when `node` is absent — the app container has none, so
these do not run there, and that gap is the same one `test_widget_markdown.py` already lives
with.

What Python covers:

| File | What it pins |
|---|---|
| `tests/unit/schemas/test_base.py` | `validate_edge_waypoints` — every refusal and every shape it must leave alone, including `True` being refused as a coordinate (Python calls it an `int`) |
| `tests/unit/schemas/flow_builder/test_flow_schemas.py` | bends survive a round trip; the cap is inclusive at four; non-finite, malformed, out-of-range and non-list bends are refused with their sentences |
| `tests/unit/schemas/graph_designer/test_graph_designer_schemas.py` | the same, plus that the bend cap does **not** quietly bound the drawing |
| `tests/unit/schemas/integrations/test_integration_schemas.py` | the same through an opaque `graph_data` |
| `tests/unit/routes/*/` | **script order** — `graph_canvas.js` before both shared modules before the canvas file, on all three pages, plus both shared stylesheets being linked |

The script-order tests are the one place a Python test catches a real JavaScript failure. Get the
order wrong and the page comes up with an empty canvas and one `undefined` in the console, with
nothing in any server log — and it is exactly the sort of thing an edit reorders without noticing.

**The extraction of `graph_edges.js` bought the first real coverage this code has ever had**,
and that is the strongest argument for having done it. A factory with an injected config can be
driven from `node` against a stubbed DOM — `tests/unit/static_js/test_graph_edges.py`, with the
stub itself in `tests/unit/static_js/support/dom_stub.js`; the closures it replaced could not be
reached at all, because `flow_builder.js` and `graph_designer.js` export only `{ init }`. The
suite asserts the things that were previously only assertable by hand:

- the anchor cache measures a **stationary** end once and never again, and tracks a **moving**
  end by arithmetic with *zero* measurements after the first — which is the whole point of it;
- a node deleted mid-drag yields `null` rather than throwing;
- `returnLaneX` memoises inside a gesture and re-reads outside one;
- `updateEdgeGeometry` puts the ✕ and the + 22px apart, and `chromeYOffset` moves both by
  exactly 10 — the one visual difference between the two canvases;
- a connector whose group has gone is re-rendered rather than skipped;
- the bend machine: press-and-release leaves no bend behind, press-and-move past the threshold
  keeps one, the fifth bend is refused *with its sentence*, a derived jump cannot be bent;
- group move carries the bends of connectors whose **both** ends move and leaves the others
  alone, works from the captured start rather than accumulating, and clamps at zero.

Three pieces of the insert feature are covered for the same reason, in
`tests/unit/static_js/test_port_splice.py` and `tests/unit/static_js/test_graph_insert.py`.
`GC.continuationPort` has assertions for every port shape the three canvases declare — the
`["body", "done"]` loop case above especially. The splice itself is reproduced against the real
port tables — transcribed from each canvas's own vocabulary, not loaded from the multi-thousand-
line canvas files, which have no `module.exports` to load — to check that the original connector
is gone, that exactly two replace it, that the source's own port is preserved rather than reset
to `default`, that splicing twice in the same place nests correctly, and that the catalogue
filter excludes Start, End Flow, Goto, Menu and Dropdown and offers the other nine. And
`graph_insert.js`'s menu itself is driven directly: the choices it renders, arrow-key navigation
that wraps, Escape and an outside press both closing it, the menu closing *before* `onChoose`
runs, and placement staying clear of the viewport's edges.

None of this reaches `flow_builder.js`, `graph_designer.js` or `integrations.js` themselves —
`renderNode`, `renderEdge`, `init`, the palette, save and load stay exactly as unreachable as
the rest of this section says, and a real JS test runner remains the honest follow-up for that.
What changed is that the connector layer and the insert menu no longer have to be that follow-up
too.

### The manual checklist

Run these per canvas. They are the interactions that will break.

1. Drag a box **right-to-left and bottom-to-top** — the same things are selected. Negative width
   is the classic marquee bug.
2. Box-select three boxes and release — **the selection survives the click behind it**.
3. Press empty canvas and release without moving — the selection clears, exactly as before.
4. Scroll the canvas right and down, then draw a box — the right boxes are caught, not ones
   offset by the scroll.
5. Group-drag toward the left edge — boxes stop at the wall **without collapsing onto x = 0**.
6. Group-drag, then add a box — nothing **flies back** to the server's arrangement.
7. Press a selected box and release without moving — nothing is marked unsaved, and the Tidy up
   button (the visible proxy for `state.layout`) does not light up.
8. Graph Designer: drag out of an output pill, and drag a connector's end handle — both still
   work, no box appears, no group moves. Shift-click still picks for a test run and the count
   agrees with the highlighting.
9. Flow Builder: group-drag a Goto box — its dashed jump follows and nothing throws. That group
   has no hit path and no handles.
10. Group-drag two boxes so one ends up above the one feeding it — the wire takes the return lane
    rather than folding over itself.
11. **No 1px jump in the wires at the start or end of a drag** — this is what proves the
    anchor-offset derivation.
12. **No forced-layout warnings in the browser's Performance panel during a five-box drag** —
    this is what proves the read/write ordering.
13. Bend a wire, Save, Reload — the bend is still there. Bend one, then Tidy up — asked first,
    then straightened. Bend one, then delete a box at either end — the connector and its bend go
    together.
14. Box-select, Save, Reload — the boxes stayed put and the selection is empty, holding no ids
    that no longer exist.
15. Escape mid-move puts everything back and marks nothing unsaved.
16. Press the **+** on a connector — a menu opens beside it, arrow keys move through it, Escape
    closes it, and a click anywhere else closes it without inserting anything.
17. Insert a block into a connector — the original connector is **gone**, two replace it, and the
    new block's properties panel is open.
18. Insert into a connector leaving a **failure** port — the first half still leaves by `failed`,
    not by the success port.
19. Graph Designer: insert a **For each** — the block that used to follow is wired to `done`, not
    to `body`, so it runs once rather than once per item.
20. Graph Designer: press the + on a connector that leads into **Success** or **Failure** — no
    outcome node is offered in the menu.
21. Press the + on a connector near the **bottom or right edge** of the window — the menu stays
    on screen rather than being clipped.
22. Press the + and *do not move* — no bend appears in the wire. The + must not fall through to
    the hit path underneath.
23. Insert a block, Save, Reload — both new connectors are there and the old one is not.
