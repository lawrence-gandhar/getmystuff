/**
 * Nested Tools — the repeating rows that say which tools this one embeds.
 *
 * One row is one sub-query: *run `child`, take its `column`, and restrict this
 * query at `target`*. What `target` means follows the query mode live — a column
 * of this query in builder mode, the name of a `:placeholder` in SQL mode — so
 * switching mode re-draws the rows rather than leaving a control that means
 * nothing.
 *
 * A row also says *how*: match any of the values (one run), or run this query once
 * per value. The second reveals a second line — the name each row records its value
 * under — because that is the only field that means anything for it, and a control
 * that is always visible and usually inert reads as a field the user forgot.
 *
 * Its own file rather than a third IIFE in tool_configs.js, which is already a
 * thousand lines of query builder. Nothing is shared between them: this card is
 * about *other tools*, the builder is about columns of one query, and they meet
 * only in the form that posts both.
 *
 * Conventions copied from tool_configs.js because they are load-bearing:
 * rows are built with createElement and never innerHTML (every name here comes
 * out of the user's own database), the hidden JSON field is the single output,
 * and init is idempotent so an htmx swap cannot wire the same card twice.
 *
 * Everything here is convenience. tool_chain_service re-validates every link on
 * save — same owner, same datasource, enabled, no cycle, within the depth and
 * size caps — so a hand-posted payload is checked by the same rules a click is.
 */
(function () {
    "use strict";

    var CARD_SELECTOR = "[data-nested-tools]";
    var OPTIONS_URL = "/tool-configs/child-options";

    document.addEventListener("DOMContentLoaded", function () {
        scan(document);
    });

    document.addEventListener("htmx:load", function (event) {
        scan(event.target);
    });

    scan(document);

    /** @param {Document|Element} root */
    function scan(root) {
        if (!root || !root.querySelectorAll) return;

        if (root.matches && root.matches(CARD_SELECTOR)) init(root);
        Array.prototype.forEach.call(root.querySelectorAll(CARD_SELECTOR), init);
    }

    /** @param {Element} root */
    function init(root) {
        if (root.dataset.nestedReady === "1") return;
        root.dataset.nestedReady = "1";

        var data = readData(root);
        var state = normalise(data.children);
        var rowsField = root.querySelector("[data-nested-rows]");
        var jsonField = root.querySelector("[data-nested-json]");
        var addButton = root.querySelector("[data-nested-add]");

        // Filled by the one fetch below. Until it answers, a saved row still
        // renders — from what it holds, not from what is offered — so an edit form
        // never briefly shows an empty picker over a real link.
        var options = [];

        if (addButton) {
            addButton.addEventListener("click", function () {
                state.push(blankRow());
                render();
            });
        }

        watchQueryMode(root, render);
        loadOptions();
        render();

        /** Fetch the tools that may be embedded, then re-render with real names. */
        function loadOptions() {
            if (!data.datasource_id) return;

            var url = OPTIONS_URL +
                "?datasource_id=" + encodeURIComponent(data.datasource_id) +
                (data.exclude ? "&exclude=" + encodeURIComponent(data.exclude) : "");

            request(url).then(function (payload) {
                options = (payload && payload.tools) || [];
                render();
            }).catch(function () {
                // Left to the saved rows: the picker is a convenience, and the
                // server refuses anything invalid anyway. A failed fetch must not
                // clear links the user already has.
                options = [];
            });
        }

        function render() {
            if (!rowsField) return;

            rowsField.textContent = "";

            if (!state.length) {
                rowsField.appendChild(mutedNote(
                    "No nested tools — this query runs on its own."
                ));
                sync();
                return;
            }

            state.forEach(function (entry, index) {
                rowsField.appendChild(buildRow(entry, index));
            });

            sync();
        }

        /**
         * One row: which tool, which of its columns, and where the values land.
         */
        function buildRow(entry, index) {
            var row = element("div", "mb-3");

            var top = element("div", "row g-2 align-items-center");

            top.appendChild(labelCell("Run"));
            top.appendChild(wrap("col-md-3", toolSelect(entry, index)));
            top.appendChild(labelCell("take"));
            top.appendChild(wrap("col-md-2", columnControl(entry)));
            top.appendChild(labelCell(isSqlMode() ? "fill" : "and filter"));
            top.appendChild(wrap("col-md-2", targetControl(entry)));
            top.appendChild(wrap("col-md-2", bindingModeControl(entry, index)));
            top.appendChild(wrap("col-md-auto", removeButton(function () {
                state.splice(index, 1);
                render();
            })));

            row.appendChild(top);

            if (entry.binding_mode === "each") {
                row.appendChild(aliasLine(entry));
            }

            return row;
        }

        /**
         * How the values are used: matched all at once, or one run each.
         *
         * Changing it re-renders rather than only syncing, because the alias line
         * below appears and disappears with it.
         */
        function bindingModeControl(entry, index) {
            return pairSelect(
                [
                    ["in_list", "match any"],
                    ["each", "run once per value"],
                ],
                entry.binding_mode || "in_list",
                function (value) {
                    state[index].binding_mode = value;
                    // A list binding has no single value to record, and the server
                    // refuses an alias on one — so clearing it here is the same rule
                    // said earlier rather than a second one.
                    if (value !== "each") state[index].value_alias = "";
                    render();
                }
            );
        }

        /** The name each row records the value it was produced for under. */
        function aliasLine(entry) {
            var line = element("div", "row g-2 align-items-center mt-1");

            line.appendChild(labelCell("record the value as"));
            line.appendChild(wrap("col-md-3", textInput(
                entry.value_alias,
                "column name (optional)",
                function (value) {
                    entry.value_alias = value;
                    sync();
                }
            )));
            line.appendChild(hint(
                "Leave blank if the query already returns the value."
            ));

            return line;
        }

        /**
         * The tool or graph to embed. A saved choice survives a picker that cannot
         * offer it.
         *
         * One select for both kinds, because the operator is making one choice: what
         * runs first and supplies the values. A graph is labelled as one, since it is
         * the only entry that may stop and ask somebody a question mid-run.
         */
        function toolSelect(entry, index) {
            var choices = options.map(function (tool) {
                var label = tool.kind === "graph"
                    ? tool.tool_name + " (graph)"
                    : tool.tool_name;

                return [tool.uuid, label];
            });

            if (entry.child_id && !options.some(function (tool) {
                return tool.uuid === entry.child_id;
            })) {
                choices.unshift([entry.child_id, entry.child_name || "(saved)"]);
            }

            // A new row shows the first choice, so take it as chosen. Written
            // straight onto the entry rather than through the change handler:
            // that handler re-renders, and a re-render from inside the render
            // loop appends every row twice.
            if (!entry.child_id && choices.length) {
                entry.child_id = choices[0][0];
                entry.child_kind = kindOf(choices[0][0]);
            }

            var select = pairSelect(choices, entry.child_id, function (value) {
                state[index].child_id = value;
                // Which key this row posts under follows the chosen thing, so it is
                // read here rather than inferred at save time from a list that may
                // have been reloaded since.
                state[index].child_kind = kindOf(value);
                // A different child returns different columns, so the column beside
                // it stops meaning anything.
                state[index].child_column = "";
                render();
            });

            if (!choices.length) {
                select.appendChild(option("", "Nothing available to embed"));
                select.disabled = true;
            }

            return select;
        }

        /** Whether a uuid names a graph or a tool config, per the loaded picker. */
        function kindOf(uuid) {
            var found = options.filter(function (candidate) {
                return candidate.uuid === uuid;
            })[0];

            return (found && found.kind) === "graph" ? "graph" : "tool";
        }

        /**
         * Which column of the child's result is collected.
         *
         * A dropdown when the child's output is knowable, a text input when it is
         * not — a SQL-mode tool, or a builder tool that selects everything. The
         * chain checks a typed name against the real result when it runs, which is
         * the same bargain the routing prompt makes about a SQL tool's fields.
         */
        function columnControl(entry) {
            var tool = options.filter(function (candidate) {
                return candidate.uuid === entry.child_id;
            })[0];

            if (tool && tool.columns && tool.columns.length) {
                if (!entry.child_column) entry.child_column = tool.columns[0];

                return valueSelect(tool.columns, entry.child_column, function (value) {
                    entry.child_column = value;
                    sync();
                });
            }

            return textInput(entry.child_column, "column name", function (value) {
                entry.child_column = value;
                sync();
            });
        }

        /**
         * Where the values land in this query.
         *
         * Builder mode offers the columns of the tables this tool reads; SQL mode
         * takes the name of a placeholder, which has to be in the statement — the
         * server refuses the save otherwise rather than letting it fail at run time.
         */
        function targetControl(entry) {
            if (isSqlMode()) {
                return textInput(
                    entry.parent_reference,
                    ":name used in the SQL",
                    function (value) {
                        entry.parent_reference = value;
                        sync();
                    }
                );
            }

            var columns = parentColumns();

            if (!columns.length) {
                return textInput(entry.parent_reference, "column", function (value) {
                    entry.parent_reference = value;
                    sync();
                });
            }

            if (!entry.parent_reference) entry.parent_reference = columns[0];

            return valueSelect(columns, entry.parent_reference, function (value) {
                entry.parent_reference = value;
                sync();
            });
        }

        /**
         * The columns this query can be filtered on: the base table's bare names,
         * plus every selected table's qualified ones.
         *
         * Mirrors what `query_joins.validated_column_reference` accepts — a bare
         * name means the base table, a qualified one must be a table the query
         * reads. A column of a table that is selected but not joined is offered and
         * refused on save, with a message naming it; guessing the live join chain
         * from here would mean re-implementing the builder's state.
         */
        function parentColumns() {
            var names = [];

            (data.column_map[data.base_table] || []).forEach(function (column) {
                names.push(column);
            });

            Object.keys(data.column_map).forEach(function (table) {
                if (table === data.base_table) return;
                (data.column_map[table] || []).forEach(function (column) {
                    names.push(table + "." + column);
                });
            });

            return names;
        }

        /** Whether the form currently means to save a SQL statement. */
        function isSqlMode() {
            var form = root.closest("form");
            var option = form && form.querySelector('[data-query-mode-option="sql"]');

            return Boolean(option && option.checked);
        }

        /**
         * The rows, as the server reads them.
         *
         * A graph posts its uuid as `child_graph_id` and a tool config as `child_id`,
         * which is what `tool_config_links` stores — exactly one of the two per row. The
         * browser keeps a single `child_id` field for either, because one select writes
         * it; the split happens here, once, where the payload is built.
         */
        function sync() {
            if (!jsonField) return;

            var rows = state.filter(function (entry) {
                return entry.child_id;
            }).map(function (entry) {
                var row = {
                    child_column: entry.child_column,
                    parent_reference: entry.parent_reference,
                    binding_mode: entry.binding_mode,
                    value_alias: entry.value_alias
                };

                if (entry.child_kind === "graph") {
                    row.child_graph_id = entry.child_id;
                } else {
                    row.child_id = entry.child_id;
                }

                return row;
            });

            jsonField.value = JSON.stringify(rows);
        }
    }

    /**
     * Re-render when the query mode changes.
     *
     * The rows themselves change shape with the mode, so this is not cosmetic: a
     * placeholder name left in a column dropdown would be saved as a column.
     */
    function watchQueryMode(root, onChange) {
        var form = root.closest("form");
        if (!form) return;

        Array.prototype.forEach.call(
            form.querySelectorAll("[data-query-mode-option]"),
            function (option) {
                option.addEventListener("change", onChange);
            }
        );
    }

    // ----------------------------------------------------------------------
    // State
    // ----------------------------------------------------------------------

    function readData(root) {
        var block = root.querySelector("[data-nested-data]");
        var parsed = {};

        try {
            parsed = JSON.parse((block && block.textContent) || "{}");
        } catch (error) {
            parsed = {};
        }

        return {
            datasource_id: parsed.datasource_id || "",
            exclude: parsed.exclude || "",
            base_table: parsed.base_table || "",
            column_map: parsed.column_map || {},
            children: Array.isArray(parsed.children) ? parsed.children : [],
        };
    }

    /**
     * Saved rows as the editor holds them.
     *
     * Both child keys collapse into one `child_id`, with `child_kind` remembering which
     * it was. The select writes one field, so keeping two would mean every read having
     * to ask which one is set — and a row where both looked set would have no answer.
     */
    function normalise(children) {
        return children.map(function (entry) {
            var isGraph = Boolean(entry.child_graph_id)
                || entry.child_kind === "graph";

            return {
                child_id: text(entry.child_graph_id || entry.child_id),
                child_kind: isGraph ? "graph" : "tool",
                child_name: text(entry.child_name),
                child_column: text(entry.child_column),
                parent_reference: text(entry.parent_reference),
                binding_mode: text(entry.binding_mode) || "in_list",
                value_alias: text(entry.value_alias),
            };
        });
    }

    function blankRow() {
        return {
            child_id: "",
            child_kind: "tool",
            child_name: "",
            child_column: "",
            parent_reference: "",
            binding_mode: "in_list",
            value_alias: "",
        };
    }

    // ----------------------------------------------------------------------
    // Controls — createElement only; every name here is user data
    // ----------------------------------------------------------------------

    function valueSelect(values, selected, onChange) {
        return pairSelect(
            values.map(function (value) { return [value, value]; }),
            selected,
            onChange
        );
    }

    /**
     * A <select> over [value, label] pairs.
     *
     * A selected value the options do not contain is kept as an option of its own,
     * so a link to a tool the picker could not load still shows what it holds
     * instead of silently becoming the first choice.
     *
     * `onChange` fires on a real change only. Building a control never writes back
     * to the state it was built from — the caller settles the default first, so a
     * render cannot restart itself part-way through and append the rows twice.
     */
    function pairSelect(pairs, selected, onChange) {
        var select = element("select", "form-select form-select-sm");
        var choices = pairs.slice();

        if (selected && !choices.some(function (pair) { return pair[0] === selected; })) {
            choices.unshift([selected, selected]);
        }

        choices.forEach(function (pair) {
            select.appendChild(option(pair[0], pair[1], pair[0] === selected));
        });

        select.addEventListener("change", function () {
            onChange(select.value);
        });

        return select;
    }

    function textInput(value, placeholder, onInput) {
        var input = element("input", "form-control form-control-sm font-monospace");
        input.type = "text";
        input.value = value || "";
        input.placeholder = placeholder;
        input.addEventListener("input", function () {
            onInput(input.value.trim());
        });
        return input;
    }

    function removeButton(onClick) {
        var button = element("button", "btn btn-sm btn-outline-danger");
        button.type = "button";
        button.textContent = "×";
        button.title = "Remove this nested tool";
        button.addEventListener("click", onClick);
        return button;
    }

    function labelCell(label) {
        var cell = element("div", "col-md-auto text-muted small");
        cell.textContent = label;
        return cell;
    }

    function hint(message) {
        var cell = element("div", "col-md-auto text-muted small");
        cell.textContent = message;
        return cell;
    }

    function mutedNote(message) {
        var note = element("p", "text-muted small mb-0");
        note.textContent = message;
        return note;
    }

    function wrap(className, child) {
        var cell = element("div", className);
        cell.appendChild(child);
        return cell;
    }

    function option(value, label, selected) {
        var node = document.createElement("option");
        node.value = value;
        node.textContent = label;
        if (selected) node.selected = true;
        return node;
    }

    function element(tag, className) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        return node;
    }

    function text(value) {
        return value === null || value === undefined ? "" : String(value);
    }

    /** JSON GET, through the app's fetch wrapper when one is present. */
    function request(url) {
        if (typeof window.safeFetch === "function") {
            return window.safeFetch(url).then(function (response) {
                return response.json();
            });
        }

        return fetch(url, { headers: { Accept: "application/json" } })
            .then(function (response) { return response.json(); });
    }
})();
