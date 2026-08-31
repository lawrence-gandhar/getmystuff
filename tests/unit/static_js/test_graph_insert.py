"""
`static/js/graph_insert.js` — the "+" menu shared by all three canvases, exercised
through `window.GraphInsert.create(config)` against a stubbed DOM.

What it owns is the menu: where it opens, how it is dismissed, keyboard navigation, and
the empty-catalogue message. What may be inserted is each canvas's own decision — see
`test_port_splice.py` for that half, which reproduces the port-table logic these tests
never touch.
"""

from __future__ import annotations

import shutil

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is needed to execute the canvas connector runtime",
)

_HARNESS = """
function makeInsertConfig(overrides) {
    const wrapperEl = new FakeElement("div");
    const chosen = [];
    let menu = null;
    const config = Object.assign({
        wrapperEl: wrapperEl,
        getChoices: function () {
            return [{ type: "a", label: "Block A" }, { type: "b", label: "Block B" }];
        },
        onChoose: function (type, edgeId) {
            chosen.push({ type: type, edgeId: edgeId, openWhileChoosing: menu.isOpen() });
        },
    }, overrides || {});
    menu = window.GraphInsert.create(config);
    return { menu: menu, chosen: chosen, config: config };
}

function choiceButtons() {
    return document.body.querySelectorAll("[data-insert-type]");
}
"""


def _run(run_js, body: str) -> None:
    run_js(_HARNESS + "\n" + body, sources=("graph_insert.js",))


class TestTheMenu:
    def test_one_button_per_choice_labelled_from_the_catalogue(self, run_js) -> None:
        _run(run_js, """
            const h = makeInsertConfig();
            h.menu.openFor("e1", 100, 100);

            const buttons = choiceButtons();
            assert.strictEqual(buttons.length, 2);
            assert.strictEqual(buttons[0].dataset.insertType, "a");
            assert.strictEqual(buttons[1].dataset.insertType, "b");

            const label = buttons[0].children.filter(function (c) { return c.nodeType === 3; })
                .map(function (c) { return c.textContent; }).join("");
            assert.strictEqual(label, "Block A");
            assert.ok(h.menu.isOpen());
        """)

    def test_an_empty_catalogue_shows_its_message_rather_than_opening_blank(self, run_js) -> None:
        _run(run_js, """
            const h = makeInsertConfig({
                getChoices: function () { return []; },
                emptyMessage: "Nothing to add.",
            });
            h.menu.openFor("e2", 50, 50);

            assert.strictEqual(choiceButtons().length, 0);
            const note = document.body.querySelector(".gc-insert-empty");
            assert.ok(note);
            assert.strictEqual(note.textContent, "Nothing to add.");
        """)

    def test_arrow_keys_move_through_the_choices_and_wrap(self, run_js) -> None:
        _run(run_js, """
            const h = makeInsertConfig();
            h.menu.openFor("e3", 10, 10);
            const buttons = choiceButtons();

            assert.strictEqual(document.activeElement, buttons[0], "the first choice is focused on open");

            document.dispatch("keydown", { key: "ArrowDown", preventDefault: function () {}, stopPropagation: function () {} });
            assert.strictEqual(document.activeElement, buttons[1]);

            document.dispatch("keydown", { key: "ArrowDown", preventDefault: function () {}, stopPropagation: function () {} });
            assert.strictEqual(document.activeElement, buttons[0], "arrow-down wraps past the last choice");

            document.dispatch("keydown", { key: "ArrowUp", preventDefault: function () {}, stopPropagation: function () {} });
            assert.strictEqual(document.activeElement, buttons[1], "arrow-up wraps past the first choice");
        """)

    def test_escape_closes_the_menu(self, run_js) -> None:
        _run(run_js, """
            const h = makeInsertConfig();
            h.menu.openFor("e4", 10, 10);
            assert.ok(h.menu.isOpen());

            document.dispatch("keydown", { key: "Escape", preventDefault: function () {}, stopPropagation: function () {} });
            assert.ok(!h.menu.isOpen());
        """)

    def test_choosing_closes_the_menu_before_the_callback_runs(self, run_js) -> None:
        _run(run_js, """
            const h = makeInsertConfig();
            h.menu.openFor("e5", 10, 10);
            const buttons = choiceButtons();

            buttons[0].dispatch("click", { preventDefault: function () {}, stopPropagation: function () {} });

            assert.strictEqual(h.chosen.length, 1);
            assert.strictEqual(h.chosen[0].type, "a");
            assert.strictEqual(h.chosen[0].edgeId, "e5");
            assert.strictEqual(h.chosen[0].openWhileChoosing, false, "the menu is closed before onChoose runs");
            assert.ok(!h.menu.isOpen());
        """)

    def test_a_press_outside_the_menu_closes_it(self, run_js) -> None:
        _run(run_js, """
            const h = makeInsertConfig();
            h.menu.openFor("e6", 10, 10);
            assert.ok(h.menu.isOpen());

            document.dispatch("pointerdown", { target: document.body });
            assert.ok(!h.menu.isOpen(), "a press outside the menu closes it");
        """)

    def test_placement_stays_inside_the_viewport(self, run_js) -> None:
        _run(run_js, """
            const h = makeInsertConfig();

            h.menu.openFor("e7", 5, 5);
            let menuEl = document.body.querySelector(".gc-insert-menu");
            assert.strictEqual(menuEl.style.left, "8px", "stays clear of the left edge");
            assert.strictEqual(menuEl.style.top, "8px", "stays clear of the top edge");

            h.menu.openFor("e7", 5000, 5000);
            menuEl = document.body.querySelector(".gc-insert-menu");
            assert.strictEqual(menuEl.style.left, "1192px", "never placed past the right edge");
            assert.strictEqual(menuEl.style.top, "792px", "never placed past the bottom edge");
        """)
