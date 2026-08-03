/**
 * Tool Configs — the query builder inside the New/Edit Tool Config offcanvas.
 *
 * Joins, columns, aggregations, groupings and filters are edited as repeating rows
 * and submitted as a single JSON field (`config_json`), so the server has exactly
 * one place to parse and validate their shape instead of reassembling parallel form
 * arrays. Same convention as the action parameter rows in chatbot_ai_settings.js,
 * and as the base_config field on the Configurations page.
 *
 * Three things stay in step with the rows: the JSON field, the generated-SQL
 * preview, and nothing else — the preview is for reading only and is never
 * executed. The server builds the same string independently for the list page
 * (tool_config_service.build_query_preview).
 *
 * Joins are offered only for a relational datasource; the server decides that and
 * says so by sending a non-empty `join_types` (app.utils.query_joins). Two rules
 * follow from a join existing:
 *
 *   1. Every column reference becomes `table.column`, because with two tables in
 *      play a bare `id` is ambiguous. Adding the first join qualifies what is
 *      already there; removing the last one unqualifies it again, so nothing the
 *      user selected is lost either way.
 *   2. A joined table's columns have to be fetched (GET /tool-configs/columns) —
 *      which table that is only becomes known when the user picks it.
 *
 * Row markup is built with createElement and value assignment rather than
 * innerHTML: table and column names come from the user's own database and must
 * never be re-parsed as markup.
 *
 * The builder is re-initialised on every htmx swap, because both the form (opening
 * the offcanvas) and the builder alone (changing the table) arrive as swapped
 * content. `data-builder-ready` keeps that idempotent.
 *
 * Everything here is convenience. tool_config_service re-validates the submitted
 * payload — the field names, the join chain, the aggregation functions, the
 * operators and the size of every list — whatever this file happens to have
 * produced.
 */
(function () {
    "use strict";

    var BUILDER_SELECTOR = "[data-query-builder]";
    var SECTIONS = ["joins", "columns", "aggregations", "group_by", "filters"];

    // Sections whose dropdowns are built from the available column references, so
    // they all have to be re-rendered when the set of joined tables changes.
    var COLUMN_SECTIONS = ["columns", "aggregations", "group_by", "filters"];

    scan(document);
    document.addEventListener("DOMContentLoaded", function () {
        scan(document);
    });
    document.addEventListener("htmx:load", function (event) {
        scan(event.target);
    });

    /**
     * Initialise every not-yet-initialised builder inside a freshly swapped node.
     * @param {Element|Document} root
     */
    function scan(root) {
        if (!root || !root.querySelectorAll) return;

        if (root.matches && root.matches(BUILDER_SELECTOR)) init(root);
        Array.prototype.forEach.call(root.querySelectorAll(BUILDER_SELECTOR), init);
    }

    /** @param {Element} root */
    function init(root) {
        if (root.dataset.builderReady === "1") return;
        root.dataset.builderReady = "1";

        var data = readData(root);
        var state = normaliseConfig(data.config);
        var jsonField = root.querySelector("[data-builder-json]");
        var previewField = root.querySelector("[data-builder-preview]");
        var noticeField = root.querySelector("[data-builder-notice]");

        // table name → its columns. Seeded with what the server rendered (the base
        // table, plus every table a saved query already joins) and filled in as
        // further tables are joined.
        var columnMap = data.column_map;
        var pendingTables = {};

        Array.prototype.forEach.call(
            root.querySelectorAll("[data-builder-add]"),
            function (button) {
                button.addEventListener("click", function () {
                    var section = button.getAttribute("data-builder-add");
                    if (section === "joins") {
                        addJoin();
                        return;
                    }
                    state[section].push(blankEntry(section));
                    renderSection(section);
                });
            }
        );

        SECTIONS.forEach(renderSection);
        sync();

        // ------------------------------------------------------------------
        // Rendering
        // ------------------------------------------------------------------

        /** Re-render one section's rows, then refresh the JSON field and preview. */
        function renderSection(section) {
            var container = root.querySelector('[data-builder-rows="' + section + '"]');
            if (!container) return;

            container.textContent = "";

            if (state[section].length === 0) {
                container.appendChild(mutedNote(emptyLabel(section)));
            } else {
                if (section === "joins") container.appendChild(joinHeader());
                state[section].forEach(function (entry, index) {
                    container.appendChild(buildRow(section, entry, index));
                });
            }

            sync();
        }

        /** Re-render everything built from column references. */
        function renderColumnSections() {
            COLUMN_SECTIONS.forEach(renderSection);
        }

        /**
         * One row. Each control writes straight back into the state entry and
         * refreshes the JSON field and preview on change.
         */
        function buildRow(section, entry, index) {
            if (section === "joins") return buildJoinRow(entry, index);

            var row = element("div", "row g-2 mb-2 align-items-center");

            if (section === "columns") {
                row.appendChild(wrap("col-5", columnSelect(entry.column, function (value) {
                    entry.column = value;
                })));
                row.appendChild(wrap("col-5", aliasInput(entry.alias, function (value) {
                    entry.alias = value;
                })));
            } else if (section === "aggregations") {
                row.appendChild(wrap("col-3", pairSelect(
                    data.aggregation_functions, entry.type, function (value) {
                        entry.type = value;
                    }
                )));
                row.appendChild(wrap("col-4", columnSelect(entry.column, function (value) {
                    entry.column = value;
                })));
                row.appendChild(wrap("col-3", aliasInput(entry.alias, function (value) {
                    entry.alias = value;
                })));
            } else if (section === "group_by") {
                row.appendChild(wrap("col-10", columnSelect(entry, function (value) {
                    state.group_by[index] = value;
                })));
            } else {
                row.appendChild(wrap("col-4", columnSelect(entry.column, function (value) {
                    entry.column = value;
                })));
                row.appendChild(wrap("col-3", valueSelect(
                    data.filter_operators, entry.operator, function (value) {
                        entry.operator = value;
                    }
                )));
                row.appendChild(wrap("col-3", textInput(
                    entry.value, "Value", function (value) {
                        entry.value = value;
                    }
                )));
            }

            row.appendChild(wrap("col-2 text-end", removeButton(function () {
                state[section].splice(index, 1);
                renderSection(section);
            })));

            return row;
        }

        /** Column labels above the join rows — five dropdowns need naming. */
        function joinHeader() {
            var row = element("div", "row g-2 mb-1 small text-muted fw-semibold");

            row.appendChild(labelCell("col-md-2", "Join type"));
            row.appendChild(labelCell("col-md-2", "Table to join"));
            row.appendChild(labelCell("col-md-4", "Matching table and column"));
            row.appendChild(labelCell("col-md-3", "= joined table column"));
            row.appendChild(labelCell("col-md-1", ""));

            return row;
        }

        /**
         * One join: type, the table joined in, and the ON condition matching a table
         * already in the query against it.
         */
        function buildJoinRow(entry, index) {
            var row = element("div", "row g-2 mb-2 align-items-center");

            row.appendChild(wrap("col-md-2", pairSelect(
                data.join_types, entry.type, function (value) {
                    entry.type = value;
                }
            )));

            row.appendChild(wrap("col-md-2", valueSelect(
                joinTableChoices(index), entry.table, function (value) {
                    changeJoinTable(entry, value);
                }
            )));

            // The ON condition's left side: a table already in the query, and one of
            // its columns.
            var condition = element("div", "row g-2");
            condition.appendChild(wrap("col-6", valueSelect(
                tablesBefore(index), entry.left_table, function (value) {
                    entry.left_table = value;
                    entry.left_column = firstColumnOf(value);
                    ensureColumns(value);
                    renderSection("joins");
                }
            )));
            condition.appendChild(wrap("col-6", valueSelect(
                columnsOf(entry.left_table), entry.left_column, function (value) {
                    entry.left_column = value;
                }
            )));
            row.appendChild(wrap("col-md-4", condition));

            row.appendChild(wrap("col-md-3", valueSelect(
                columnsOf(entry.table), entry.right_column, function (value) {
                    entry.right_column = value;
                }
            )));

            row.appendChild(wrap("col-md-1 text-end", removeButton(function () {
                removeJoin(index);
            })));

            return row;
        }

        // ------------------------------------------------------------------
        // Joins
        // ------------------------------------------------------------------

        /** Add a join onto the last table in the chain and load its columns. */
        function addJoin() {
            var table = joinTableChoices(state.joins.length)[0];
            if (!table) {
                showNotice("Every other table in this datasource is already joined.");
                return;
            }

            var leftTable = lastTable();
            var wasUnjoined = state.joins.length === 0;

            var leftColumn = firstColumnOf(leftTable);

            state.joins.push({
                type: (data.join_types[0] || ["inner"])[0],
                table: table,
                left_table: leftTable,
                left_column: leftColumn,
                // Filled in by backfillColumns once this table's columns arrive.
                right_column: matchingColumn(table, leftTable, leftColumn),
            });

            // The query now reads more than one table, so every reference to the
            // base table has to say so.
            if (wasUnjoined) qualifyReferences();

            renderSection("joins");
            renderColumnSections();
            ensureColumns(table);
        }

        /** Point a join at a different table, reloading that table's columns. */
        function changeJoinTable(entry, value) {
            var previous = entry.table;
            entry.table = value;
            entry.right_column = matchingColumn(
                value, entry.left_table, entry.left_column,
            );

            // Anything selected from the table this join used to bring in is gone
            // with it, and so is any join that matched against it.
            dropTables([previous]);

            renderSection("joins");
            renderColumnSections();
            ensureColumns(value);
        }

        /**
         * Remove one join — and with it any join that matched against the table it
         * brought in, plus every column reference to those tables. Reported rather
         * than done quietly: the user is losing selections they made.
         */
        function removeJoin(index) {
            var removed = state.joins[index].table;
            state.joins.splice(index, 1);

            var dropped = dropTables([removed]);

            if (state.joins.length === 0) unqualifyReferences();

            renderSection("joins");
            renderColumnSections();

            showNotice(dropped
                ? "Removed the join on '" + removed + "', along with " + dropped +
                  " selection(s) that referred to a table it brought in."
                : "");
        }

        /**
         * Drop everything that depends on the given tables: joins matching against
         * them (and, in turn, the tables those joins brought in), and every column
         * reference qualified with any of them.
         *
         * @returns {number} how many column references were dropped
         */
        function dropTables(tables) {
            var gone = tables.slice();
            var searching = true;

            while (searching) {
                searching = false;
                state.joins = state.joins.filter(function (join) {
                    if (gone.indexOf(join.left_table) === -1) return true;
                    gone.push(join.table);
                    searching = true;
                    return false;
                });
            }

            // A table is only really gone if no remaining join brings it back in.
            var live = queryTables();
            var dead = gone.filter(function (table) {
                return live.indexOf(table) === -1;
            });
            if (dead.length === 0) return 0;

            var dropped = 0;

            COLUMN_SECTIONS.forEach(function (section) {
                var kept = state[section].filter(function (entry) {
                    var reference = section === "group_by" ? entry : entry.column;
                    return dead.indexOf(tableOf(reference)) === -1;
                });
                dropped += state[section].length - kept.length;
                state[section] = kept;
            });

            return dropped;
        }

        /** Every table this query reads: the base table plus each joined one. */
        function queryTables() {
            return [data.base_table].concat(state.joins.map(function (join) {
                return join.table;
            }));
        }

        /** The table most recently joined — what a new join matches against. */
        function lastTable() {
            var tables = queryTables();
            return tables[tables.length - 1];
        }

        /** The tables a join at this position may match against: the ones before it. */
        function tablesBefore(index) {
            return [data.base_table].concat(
                state.joins.slice(0, index).map(function (join) {
                    return join.table;
                })
            );
        }

        /** Tables still available to join at this position. */
        function joinTableChoices(index) {
            var taken = state.joins
                .filter(function (_, position) { return position !== index; })
                .map(function (join) { return join.table; });

            return data.join_tables.filter(function (table) {
                return taken.indexOf(table) === -1;
            });
        }

        // ------------------------------------------------------------------
        // Column references
        // ------------------------------------------------------------------

        /**
         * The column references the dropdowns offer: bare names while the query
         * reads one table, `table.column` once it reads more than one.
         */
        function columnOptions() {
            if (state.joins.length === 0) return columnsOf(data.base_table);

            var references = [];
            queryTables().forEach(function (table) {
                columnsOf(table).forEach(function (column) {
                    references.push(table + "." + column);
                });
            });

            return references;
        }

        /** The columns of one table, as far as they have been loaded. */
        function columnsOf(table) {
            var columns = table && columnMap[table];
            return Array.isArray(columns) ? columns : [];
        }

        function firstColumnOf(table) {
            return columnsOf(table)[0] || "";
        }

        /**
         * The column on the joined table most likely to be the match: a foreign key
         * named after the table it is matched against (`customers.id` →
         * `customer_id`), the same column name, or failing both its first column.
         *
         * A starting point only — the dropdown is right there. It exists because the
         * one-click case is overwhelmingly a foreign key, and offering column one of
         * the table instead reads as a wrong answer.
         */
        function matchingColumn(table, leftTable, leftColumn) {
            var columns = columnsOf(table);
            var candidates = [
                leftTable + "_" + leftColumn,
                leftTable.replace(/s$/, "") + "_" + leftColumn,
                leftColumn,
            ];

            for (var i = 0; i < candidates.length; i++) {
                if (columns.indexOf(candidates[i]) !== -1) return candidates[i];
            }

            return firstColumnOf(table);
        }

        /**
         * Load a table's columns if they are not already known, then re-render
         * whatever was waiting on them. Failures are reported in the builder rather
         * than swallowed — a dropdown that is silently empty looks like a table with
         * no columns.
         */
        function ensureColumns(table) {
            if (!table || columnMap[table] || pendingTables[table]) return;
            if (!data.datasource_id) return;

            pendingTables[table] = true;

            var url = "/tool-configs/columns?datasource_id=" +
                encodeURIComponent(data.datasource_id) +
                "&table_name=" + encodeURIComponent(table);

            request(url)
                .then(function (response) { return response.json(); })
                .then(function (payload) {
                    columnMap[table] = payload.columns || [];
                    if (payload.error) {
                        showNotice("Could not read the columns of '" + table + "': " +
                            payload.error);
                    }
                    backfillColumns();
                    renderSection("joins");
                    renderColumnSections();
                })
                .catch(function () {
                    showNotice("Could not read the columns of '" + table +
                        "'. Check the datasource connection and try again.");
                })
                .then(function () {
                    delete pendingTables[table];
                });
        }

        /** Give any join row still missing a column one, now that they are loaded. */
        function backfillColumns() {
            state.joins.forEach(function (join) {
                if (!join.left_column) join.left_column = firstColumnOf(join.left_table);
                if (!join.right_column) {
                    join.right_column = matchingColumn(
                        join.table, join.left_table, join.left_column,
                    );
                }
            });
        }

        /** Qualify every bare reference with the base table. */
        function qualifyReferences() {
            mapReferences(function (reference) {
                if (!reference || reference.indexOf(".") !== -1) return reference;
                return data.base_table + "." + reference;
            });
        }

        /** Strip the base table back off every reference that carries it. */
        function unqualifyReferences() {
            var prefix = data.base_table + ".";
            mapReferences(function (reference) {
                return reference && reference.indexOf(prefix) === 0
                    ? reference.slice(prefix.length)
                    : reference;
            });
        }

        /** Rewrite the column reference of every row in every column section. */
        function mapReferences(rewrite) {
            state.columns.forEach(function (entry) {
                entry.column = rewrite(entry.column);
            });
            state.aggregations.forEach(function (entry) {
                entry.column = rewrite(entry.column);
            });
            state.group_by = state.group_by.map(rewrite);
            state.filters.forEach(function (entry) {
                entry.column = rewrite(entry.column);
            });
        }

        // ------------------------------------------------------------------
        // Output
        // ------------------------------------------------------------------

        /**
         * Keep the JSON field and the SQL preview in step with the rows.
         *
         * The JSON field is editable by hand as well (matching the base_config field
         * on the Configurations page), so this overwrites manual edits the moment a
         * row changes — the field is the builder's output, and whatever is finally
         * submitted is validated server-side either way.
         */
        function sync() {
            if (jsonField) jsonField.value = JSON.stringify(state, null, 2);
            if (previewField) previewField.textContent = buildSql(state, tableName());
        }

        /** The table the query reads, taken from the form's own Table field. */
        function tableName() {
            var form = root.closest("form");
            var field = form && form.querySelector('[name="table_name"]');
            return (field && field.value) || data.base_table || "";
        }

        function showNotice(message) {
            if (!noticeField) return;
            noticeField.textContent = message;
            noticeField.classList.toggle("d-none", !message);
        }

        // ------------------------------------------------------------------
        // Controls
        // ------------------------------------------------------------------

        function columnSelect(selected, onChange) {
            return valueSelect(columnOptions(), selected, onChange);
        }

        /**
         * <select> over plain string options.
         *
         * A selected value that is not among the options is kept as an option of its
         * own rather than being silently replaced by the first one — a config saved
         * against a table that has since changed shows what it actually holds.
         */
        function valueSelect(options, selected, onChange) {
            var select = element("select", "form-select");
            var choices = options.slice();

            if (selected && choices.indexOf(selected) === -1) choices.unshift(selected);
            if (!choices.length) choices.unshift("");

            choices.forEach(function (value) {
                select.appendChild(option(value, value || "—", value === selected));
            });

            select.addEventListener("change", function () {
                onChange(select.value);
                sync();
            });

            return select;
        }

        /** <select> over [value, label] pairs (aggregation functions, join types). */
        function pairSelect(pairs, selected, onChange) {
            var select = element("select", "form-select");

            pairs.forEach(function (pair) {
                select.appendChild(option(pair[0], pair[1], pair[0] === selected));
            });

            select.addEventListener("change", function () {
                onChange(select.value);
                sync();
            });

            return select;
        }

        function aliasInput(value, onChange) {
            return textInput(value, "Alias (optional)", onChange);
        }

        function textInput(value, placeholder, onChange) {
            var input = element("input", "form-control");
            input.type = "text";
            input.placeholder = placeholder;
            input.value = value || "";

            input.addEventListener("input", function () {
                onChange(input.value);
                sync();
            });

            return input;
        }

        /** A blank row for one of the column sections. */
        function blankEntry(section) {
            var firstColumn = columnOptions()[0] || "";

            if (section === "group_by") return firstColumn;
            if (section === "columns") return { column: firstColumn, alias: "" };
            if (section === "aggregations") {
                var functions = data.aggregation_functions[0];
                return {
                    type: functions ? functions[0] : "count",
                    column: firstColumn,
                    alias: "",
                };
            }
            return {
                column: firstColumn,
                operator: data.filter_operators[0] || "=",
                value: "",
            };
        }
    }

    // ----------------------------------------------------------------------
    // SQL preview
    // ----------------------------------------------------------------------

    /**
     * Render the query as readable SQL. Display only — it is never executed, and it
     * mirrors tool_config_service.build_query_preview so the form and the list page
     * describe a config the same way.
     */
    function buildSql(state, table) {
        var selected = [];

        state.columns.forEach(function (entry) {
            if (entry.column) selected.push(withAlias(entry.column, entry.alias));
        });
        state.aggregations.forEach(function (entry) {
            if (!entry.column || !entry.type) return;
            selected.push(withAlias(
                entry.type.toUpperCase() + "(" + entry.column + ")", entry.alias
            ));
        });

        var sql = "SELECT " + (selected.length ? selected.join(", ") : "*") +
            "\n  FROM " + (table || "…");

        state.joins.forEach(function (join) {
            if (!join.table || !join.left_table) return;
            sql += "\n  " + joinKeyword(join.type) + " " + join.table +
                "\n    ON " + join.left_table + "." + (join.left_column || "…") +
                " = " + join.table + "." + (join.right_column || "…");
        });

        var conditions = state.filters
            .filter(function (entry) { return entry.column; })
            .map(function (entry) {
                return entry.column + " " + entry.operator + " '" + entry.value + "'";
            });
        if (conditions.length) sql += "\n WHERE " + conditions.join("\n   AND ");

        var grouping = state.group_by.filter(Boolean);
        if (grouping.length) sql += "\n GROUP BY " + grouping.join(", ");

        return sql;
    }

    /** The SQL keyword for a join type. Mirrors query_joins.JOIN_TYPE_SQL. */
    function joinKeyword(type) {
        if (type === "left") return "LEFT JOIN";
        if (type === "right") return "RIGHT JOIN";
        if (type === "full") return "FULL OUTER JOIN";
        return "INNER JOIN";
    }

    function withAlias(expression, alias) {
        return alias ? expression + " AS " + alias : expression;
    }

    // ----------------------------------------------------------------------
    // Data + state
    // ----------------------------------------------------------------------

    /**
     * Read the server-rendered JSON block. A missing or malformed block yields an
     * empty builder rather than a broken form.
     */
    function readData(root) {
        var script = root.querySelector("script[data-builder-data]");
        var parsed = {};

        if (script) {
            try {
                parsed = JSON.parse(script.textContent) || {};
            } catch (e) {
                parsed = {};
            }
        }

        var columnMap = parsed.column_map && typeof parsed.column_map === "object"
            ? parsed.column_map
            : {};
        var baseTable = text(parsed.base_table);

        // The base table's columns are rendered as their own list too (the template
        // uses them to decide whether the builder has anything to build with), so
        // fall back to it if the map somehow arrived without them.
        if (baseTable && !Array.isArray(columnMap[baseTable])) {
            columnMap[baseTable] = Array.isArray(parsed.columns) ? parsed.columns : [];
        }

        return {
            base_table: baseTable,
            datasource_id: text(parsed.datasource_id),
            column_map: columnMap,
            join_tables: Array.isArray(parsed.join_tables) ? parsed.join_tables : [],
            join_types: Array.isArray(parsed.join_types) ? parsed.join_types : [],
            config: parsed.config && typeof parsed.config === "object" ? parsed.config : {},
            aggregation_functions: Array.isArray(parsed.aggregation_functions)
                ? parsed.aggregation_functions
                : [],
            filter_operators: Array.isArray(parsed.filter_operators)
                ? parsed.filter_operators
                : [],
        };
    }

    /**
     * Coerce a saved config into the lists the builder edits, dropping anything
     * unrecognised. A config saved against a table that has since changed simply
     * loses the rows that no longer make sense.
     *
     * Key order matches what tool_config_service persists, so the JSON field reads
     * the same as the stored config.
     */
    function normaliseConfig(config) {
        return {
            columns: asList(config.columns).map(function (entry) {
                return { column: text(entry.column), alias: text(entry.alias) };
            }),
            aggregations: asList(config.aggregations).map(function (entry) {
                return {
                    type: text(entry.type),
                    column: text(entry.column),
                    alias: text(entry.alias),
                };
            }),
            group_by: asList(config.group_by).map(text),
            filters: asList(config.filters).map(function (entry) {
                return {
                    column: text(entry.column),
                    operator: text(entry.operator),
                    value: text(entry.value),
                };
            }),
            joins: asList(config.joins).map(function (entry) {
                return {
                    type: text(entry.type) || "inner",
                    table: text(entry.table),
                    left_table: text(entry.left_table),
                    left_column: text(entry.left_column),
                    right_column: text(entry.right_column),
                };
            }),
        };
    }

    /** The table half of a `table.column` reference, or "" when it is bare. */
    function tableOf(reference) {
        var value = text(reference);
        var separator = value.indexOf(".");
        return separator === -1 ? "" : value.slice(0, separator);
    }

    /** Session-aware fetch when the page provides one (see base/layout.htm). */
    function request(url) {
        return typeof safeFetch === "function" ? safeFetch(url) : fetch(url);
    }

    // ----------------------------------------------------------------------
    // Small DOM + value helpers
    // ----------------------------------------------------------------------

    function element(tag, className) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        return node;
    }

    function wrap(className, child) {
        var column = element("div", className);
        column.appendChild(child);
        return column;
    }

    function labelCell(className, label) {
        var cell = element("div", className);
        cell.textContent = label;
        return cell;
    }

    function option(value, label, selected) {
        var node = element("option");
        node.value = value;
        node.textContent = label;
        if (selected) node.selected = true;
        return node;
    }

    function removeButton(onClick) {
        var button = element("button", "btn btn-sm btn-danger");
        button.type = "button";
        button.title = "Remove";
        button.textContent = "✕";
        button.addEventListener("click", onClick);
        return button;
    }

    function mutedNote(message) {
        var note = element("p", "text-muted small mb-0");
        note.textContent = message;
        return note;
    }

    function emptyLabel(section) {
        if (section === "joins") return "No joins — this query reads one table.";
        if (section === "columns") return "No columns chosen — every column is selected.";
        if (section === "aggregations") return "No aggregations.";
        if (section === "group_by") return "No grouping.";
        return "No filters — every row is returned.";
    }

    function asList(value) {
        if (!Array.isArray(value)) return [];
        return value.filter(function (entry) {
            return entry !== null && entry !== undefined;
        });
    }

    function text(value) {
        return value === null || value === undefined ? "" : String(value);
    }
})();

/**
 * Tool Configs — the query mode switch inside the New/Edit Tool Config offcanvas.
 *
 * A tool config holds its query one of two ways (app.models.tool_configs): the
 * structured builder above, or one read-only SQL statement. Both panels are always
 * in the DOM and this shows one of them, so switching back and forth never costs
 * the operator what they had typed in the other — only the mode they submit
 * decides which one is stored, and the server discards the other.
 *
 * The `required` attribute moves with the visible panel. A hidden `required`
 * textarea makes the browser refuse to submit while pointing its validation bubble
 * at something that is not on screen, which reads as the form being broken.
 *
 * Re-initialised on every htmx swap for the same reason the builder is: the form
 * arrives as swapped content when the offcanvas opens, and the mode field arrives
 * on its own — out of band — when the datasource changes. `data-mode-ready` keeps
 * that idempotent.
 *
 * Nothing here is enforcement. tool_config_service validates the mode and the
 * statement it names, whatever this file happened to show.
 */
(function () {
    "use strict";

    var FIELD_SELECTOR = "[data-query-mode-field]";

    scan(document);
    document.addEventListener("DOMContentLoaded", function () {
        scan(document);
    });
    document.addEventListener("htmx:load", function (event) {
        scan(event.target);
    });

    /**
     * Initialise every not-yet-initialised mode field inside a swapped node.
     * @param {Element|Document} root
     */
    function scan(root) {
        if (!root || !root.querySelectorAll) return;

        if (root.matches && root.matches(FIELD_SELECTOR)) init(root);
        Array.prototype.forEach.call(root.querySelectorAll(FIELD_SELECTOR), init);
    }

    /** @param {Element} field */
    function init(field) {
        if (field.dataset.modeReady === "1") return;
        field.dataset.modeReady = "1";

        var options = field.querySelectorAll("[data-query-mode-option]");

        Array.prototype.forEach.call(options, function (option) {
            option.addEventListener("change", function () {
                if (option.checked) apply(field, option.value);
            });
        });

        apply(field, selectedMode(options));
    }

    /**
     * The checked mode, defaulting to the builder — which is also what a form
     * rendered before a datasource was picked submits.
     * @param {NodeList} options
     * @returns {string}
     */
    function selectedMode(options) {
        for (var i = 0; i < options.length; i += 1) {
            if (options[i].checked) return options[i].value;
        }
        return "builder";
    }

    /**
     * Show the panel for `mode` and hide the other.
     *
     * The builder panel is a sibling of the mode field rather than a child of it
     * (the field is swapped out of band on its own), so panels are looked up
     * against the whole form.
     *
     * @param {Element} field
     * @param {string} mode
     */
    function apply(field, mode) {
        var scope = field.closest("form") || document;
        var panels = scope.querySelectorAll("[data-query-mode-panel]");

        Array.prototype.forEach.call(panels, function (panel) {
            var active = panel.dataset.queryModePanel === mode;
            panel.classList.toggle("d-none", !active);
            setRequired(panel, active);
        });
    }

    /**
     * Mark the SQL textarea required only while it is the visible panel.
     * @param {Element} panel
     * @param {boolean} active
     */
    function setRequired(panel, active) {
        var textarea = panel.querySelector("#toolSqlQuery");
        if (!textarea) return;

        if (active) {
            textarea.setAttribute("required", "required");
        } else {
            textarea.removeAttribute("required");
        }
    }
})();
