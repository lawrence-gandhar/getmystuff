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
        var groupingField = root.querySelector("[data-builder-grouping]");

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
                row.appendChild(wrap("col-3", columnSelect(entry.column, function (value) {
                    entry.column = value;
                })));
                row.appendChild(wrap("col-2", valueSelect(
                    data.filter_operators, entry.operator, function (value) {
                        entry.operator = value;
                        // IS NULL and friends compare against nothing, so a value or
                        // an agent parameter left over from the previous operator
                        // would be stored as a field that provably cannot affect the
                        // query — and would read as meaningful to whoever saw it next.
                        if (takesNoValue(value)) {
                            entry.value = "";
                            entry.agent_supplied = false;
                            entry.param = "";
                            entry.description = "";
                        }
                        renderSection(section);
                    }
                )));

                // One cell, two meanings, decided by the checkbox beside it. A fixed
                // filter compares against the value typed here; an agent-supplied one
                // has no stored value at all and this names the parameter the agent
                // passes it as. Sharing the cell keeps the row one line wide, and the
                // placeholder is what says which of the two you are looking at.
                if (takesNoValue(entry.operator)) {
                    // Nothing to type and nothing for an agent to supply. The cell is
                    // kept rather than collapsed so the row stays aligned with the
                    // ones above and below it.
                    row.appendChild(wrap("col-5", mutedNote(valuelessNote(entry))));
                } else if (entry.agent_supplied) {
                    row.appendChild(wrap("col-3", textInput(
                        entry.param, "Parameter name", function (value) {
                            entry.param = value;
                        }
                    )));
                } else {
                    row.appendChild(wrap("col-3", textInput(
                        entry.value, "Value", function (value) {
                            entry.value = value;
                        }
                    )));
                }

                if (!takesNoValue(entry.operator)) row.appendChild(wrap("col-2", checkbox(
                    entry.agent_supplied, "Agent fills in",
                    "The assistant supplies this value per question, instead of it " +
                    "being fixed here. Everything else about the filter stays fixed.",
                    function (checked) {
                        entry.agent_supplied = checked;
                        // The two are mutually exclusive by construction — a filter
                        // either has a stored value or a parameter — so the one being
                        // turned off is cleared rather than left to be submitted and
                        // silently ignored.
                        if (checked) {
                            entry.value = "";
                            if (!entry.param) entry.param = defaultParamName(entry.column);
                            if (entry.required === undefined) entry.required = true;
                        } else {
                            entry.param = "";
                            entry.description = "";
                        }
                        renderSection(section);
                    }
                )));
            }

            row.appendChild(wrap("col-2 text-end", removeButton(function () {
                state[section].splice(index, 1);
                renderSection(section);
            })));

            // Wraps onto its own line — the row above is already twelve columns wide.
            // Second-line rather than squeezed in beside the rest because this is the
            // sentence the assistant is shown to decide what the parameter means, and
            // a cramped box invites the two-word version that tells it nothing.
            if (section === "filters" && entry.agent_supplied) {
                row.appendChild(wrap("col-10", textInput(
                    entry.description,
                    "What this value means, for the assistant (e.g. \"ISO date; " +
                    "only projects created after it\")",
                    function (value) { entry.description = value; }
                )));
                row.appendChild(wrap("col-2", checkbox(
                    entry.required !== false, "Required",
                    "The assistant must supply this value. Unticked, it may leave it " +
                    "out and this one filter is then not applied.",
                    function (checked) { entry.required = checked; }
                )));
            }

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
            showGroupingWarning(groupingProblem(state, tableName()));
        }

        /**
         * Say so, as the rows change, when the query groups in a way the database
         * will refuse.
         *
         * Its own element rather than showNotice(): that one carries one-off messages
         * about an action just taken ("removing this join dropped two columns") and
         * this is a standing statement about the query as it currently reads, so one
         * must not overwrite the other.
         *
         * A warning, not a block. tool_config_service._require_grouped_selection
         * refuses the save with the same reasoning, and this is the earlier, gentler
         * half of that — visible while there is still a dropdown open to fix it in.
         */
        function showGroupingWarning(message) {
            if (!groupingField) return;
            groupingField.textContent = message;
            groupingField.classList.toggle("d-none", !message);
        }

        /**
         * The primary table the query reads, taken from the form's own Tables field.
         *
         * The field is a multi-select and the *first selected* option is the primary
         * table — the one the preview's FROM clause names and every bare column
         * reference means. Reading `field.value` instead would give the first
         * selected value too, but only by accident of how a multi-select reports it,
         * so the option scan says what is meant.
         *
         * Falls back to the server-rendered base table, which is what the builder was
         * drawn from before the user touched the field.
         */
        function tableName() {
            var form = root.closest("form");
            var field = form && form.querySelector('[name="table_names"]');

            if (field && field.selectedOptions && field.selectedOptions.length) {
                return field.selectedOptions[0].value;
            }

            return data.base_table || "";
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

        function checkbox(checked, label, title, onChange) {
            var holder = element("div", "form-check mt-2");
            var input = element("input", "form-check-input");
            var caption = element("label", "form-check-label small");

            input.type = "checkbox";
            input.checked = !!checked;
            input.id = "chk-" + (checkboxSequence += 1);
            input.title = title || "";

            caption.setAttribute("for", input.id);
            caption.textContent = label;
            caption.title = title || "";

            input.addEventListener("change", function () {
                onChange(input.checked);
                sync();
            });

            holder.appendChild(input);
            holder.appendChild(caption);

            return holder;
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
                agent_supplied: false,
                param: "",
                description: "",
                required: true,
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
                // Mirrors tool_config_service._preview_condition, including the SQL a
                // value-less operator stands for — the operator label is not something
                // a database understands, and this preview is read as SQL.
                if (entry.operator === "IS BLANK") {
                    return "(" + entry.column + " IS NULL OR TRIM(" + entry.column + ") = '')";
                }
                if (entry.operator === "IS NOT BLANK") {
                    return "(" + entry.column + " IS NOT NULL AND TRIM(" + entry.column + ") <> '')";
                }
                if (takesNoValue(entry.operator)) {
                    return entry.column + " " + entry.operator;
                }
                var right = entry.agent_supplied
                    ? ":" + (entry.param || defaultParamName(entry.column) || "value")
                    : "'" + entry.value + "'";
                return entry.column + " " + entry.operator + " " + right;
            });
        if (conditions.length) sql += "\n WHERE " + conditions.join("\n   AND ");

        var grouping = state.group_by.filter(Boolean);
        if (grouping.length) sql += "\n GROUP BY " + grouping.join(", ");

        return sql;
    }

    /**
     * Why this query's grouping would be refused by the database, or "" when it is
     * sound.
     *
     * Mirrors tool_config_service._require_grouped_selection, wording included, so
     * the form says the same thing before the save that the server says if it is
     * saved anyway. Once a query aggregates or groups, MySQL (ONLY_FULL_GROUP_BY) and
     * PostgreSQL accept only columns that are grouped or aggregated — and an empty
     * Columns list means every column, so a grouped query that chooses none is the
     * same fault written shorter.
     */
    function groupingProblem(state, table) {
        var aggregations = state.aggregations.filter(function (entry) {
            return entry.column && entry.type;
        });
        var grouping = state.group_by.filter(Boolean);
        var columns = state.columns.filter(function (entry) { return entry.column; });

        if (!aggregations.length && !grouping.length) return "";

        if (grouping.length && !columns.length && !aggregations.length) {
            return "This query groups rows but selects every column, which the " +
                "database will refuse. Add the grouped columns and the aggregations " +
                "you want to Columns and Aggregations, or remove the grouping.";
        }

        var grouped = grouping.map(function (reference) {
            return groupingKey(reference, table);
        });

        for (var index = 0; index < columns.length; index++) {
            var reference = columns[index].column;

            if (grouped.indexOf(groupingKey(reference, table)) === -1) {
                return "Column '" + reference + "' is selected but not grouped. A " +
                    "query that aggregates can only select columns that are also in " +
                    "Group By — add '" + reference + "' to Group By, aggregate it " +
                    "instead, or remove it from Columns.";
            }
        }

        return "";
    }

    /**
     * One column reference in the single form the grouping check compares. Mirrors
     * tool_config_service._grouping_key: a bare name means the base table, which is
     * how the builder writes references until a join is added.
     */
    function groupingKey(reference, table) {
        var name = String(reference || "").trim().toLowerCase();

        return name.indexOf(".") !== -1
            ? name
            : String(table || "").trim().toLowerCase() + "." + name;
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
                    agent_supplied: !!entry.agent_supplied,
                    param: text(entry.param),
                    description: text(entry.description),
                    // Absent means required — the server defaults the same way, so a
                    // config saved before this feature reopens with the safer answer.
                    required: entry.required !== false,
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

    // Unique ids for checkbox/label pairs. A label only activates its input when the
    // `for` matches an id, and every filter row builds its own.
    var checkboxSequence = 0;

    // Operators with no right-hand side. Kept in step with
    // app/models/tool_configs/tool_configs.py VALUELESS_FILTER_OPERATORS — a test
    // asserts the two lists match, because a mismatch shows up as a value box the
    // server then rejects, or a missing one the server then demands.
    var VALUELESS_OPERATORS = ["IS NULL", "IS NOT NULL", "IS BLANK", "IS NOT BLANK"];

    function takesNoValue(operator) {
        return VALUELESS_OPERATORS.indexOf(String(operator || "")) !== -1;
    }

    /** What the row says where the value box would have been. */
    function valuelessNote(entry) {
        if (entry.operator === "IS BLANK") return "matches null, empty and blank";
        if (entry.operator === "IS NOT BLANK") return "excludes null, empty and blank";
        if (entry.operator === "IS NULL") return "matches rows with no value";
        return "matches rows that have a value";
    }

    /** The parameter name a column suggests: "projects.created_at" -> "created_at". */
    function defaultParamName(column) {
        var tail = String(column || "").split(".").pop();
        return tail.replace(/[^0-9a-zA-Z_]/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
    }

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

/**
 * Values the assistant supplies — the repeating rows inside the SQL panel.
 *
 * One row declares one `:name` the statement uses and the model fills in. What is
 * saved is `{param, type, required, description}`; the *test value* beside it is
 * not — it goes into its own hidden field, is read by Test Query, and never reaches
 * the tool config. That split is the point: a statement holding `:department_id`
 * cannot be tested without a value, and the only honest value is one the operator
 * typed.
 *
 * Its own IIFE, beside the query-mode one, for the same reason tool_chain.js is its
 * own file: nothing is shared with the query builder above, which is about columns
 * of one query rather than about arguments to it.
 *
 * Conventions copied from the rest of this file because they are load-bearing: rows
 * are built with createElement and never innerHTML, one hidden JSON field is the
 * only output, and init is idempotent so an htmx swap cannot wire the same card
 * twice. Everything here is convenience — tool_config_service.validated_sql_params
 * re-checks every name against the statement on save.
 */
(function () {
    "use strict";

    var CARD_SELECTOR = "[data-sql-params]";
    var TYPES = [
        ["text", "Text"],
        ["number", "Number"],
        ["boolean", "True / false"],
    ];

    scan(document);
    document.addEventListener("DOMContentLoaded", function () {
        scan(document);
    });
    document.addEventListener("htmx:load", function (event) {
        scan(event.target);
    });

    /** @param {Document|Element} root */
    function scan(root) {
        if (!root || !root.querySelectorAll) return;

        if (root.matches && root.matches(CARD_SELECTOR)) init(root);
        Array.prototype.forEach.call(root.querySelectorAll(CARD_SELECTOR), init);
    }

    /** @param {Element} card */
    function init(card) {
        if (card.dataset.sqlParamsReady === "1") return;
        card.dataset.sqlParamsReady = "1";

        var scope = card.closest("[data-query-mode-panel]") ||
            card.closest("form") || document;
        var rowsField = card.querySelector("[data-sql-params-rows]");
        var jsonField = scope.querySelector("[data-sql-params-json]");
        var testField = scope.querySelector("[data-sql-params-test-json]");
        var addButton = card.querySelector("[data-sql-params-add]");
        var state = read(jsonField);

        if (addButton) {
            addButton.addEventListener("click", function () {
                state.push(blank());
                render();
            });
        }

        render();

        function render() {
            if (!rowsField) return;

            rowsField.textContent = "";

            if (!state.length) {
                rowsField.appendChild(note(
                    "None — this tool takes no arguments and runs the statement as " +
                    "written."
                ));
                sync();
                return;
            }

            state.forEach(function (entry, index) {
                rowsField.appendChild(row(entry, index));
            });

            sync();
        }

        function row(entry, index) {
            var wrapper = element("div", "mb-3");
            var top = element("div", "row g-2 align-items-center");

            top.appendChild(label("Name"));
            top.appendChild(cell("col-md-3", input(
                entry.param, "department_id", "font-monospace",
                function (value) { entry.param = value; sync(); }
            )));
            top.appendChild(label("holds"));
            top.appendChild(cell("col-md-2", select(
                TYPES, entry.type || "text",
                function (value) { entry.type = value; sync(); }
            )));
            top.appendChild(cell("col-md-auto", check(
                "Required", entry.required !== false,
                function (value) { entry.required = value; sync(); }
            )));
            top.appendChild(label("test with"));
            top.appendChild(cell("col-md-2", input(
                entry.test_value, "value for Test Query", "",
                function (value) { entry.test_value = value; sync(); }
            )));
            top.appendChild(cell("col-md-auto", remove(function () {
                state.splice(index, 1);
                render();
            })));

            var bottom = element("div", "row g-2 align-items-center mt-1");
            bottom.appendChild(label("meaning"));
            bottom.appendChild(cell("col", input(
                entry.description,
                "what this value is, so the assistant knows when to supply it",
                "",
                function (value) { entry.description = value; sync(); }
            )));

            wrapper.appendChild(top);
            wrapper.appendChild(bottom);

            return wrapper;
        }

        /**
         * Write both fields.
         *
         * `test_value` is stripped from what is saved and put in the other field
         * instead — one loop rather than two so the two cannot disagree about which
         * row a value belongs to.
         */
        function sync() {
            var named = state.filter(function (entry) { return entry.param; });

            if (jsonField) {
                jsonField.value = JSON.stringify(named.map(function (entry) {
                    return {
                        param: entry.param,
                        type: entry.type || "text",
                        required: entry.required !== false,
                        description: entry.description || "",
                    };
                }));
            }

            if (testField) {
                var values = {};
                named.forEach(function (entry) {
                    if (entry.test_value) values[entry.param] = entry.test_value;
                });
                testField.value = JSON.stringify(values);
            }
        }
    }

    // ----------------------------------------------------------------------
    // State
    // ----------------------------------------------------------------------

    function read(field) {
        var parsed = [];

        try {
            parsed = JSON.parse((field && field.value) || "[]");
        } catch (error) {
            parsed = [];
        }

        if (!Array.isArray(parsed)) return [];

        return parsed.map(function (entry) {
            return {
                param: text(entry.param),
                type: text(entry.type) || "text",
                required: entry.required !== false,
                description: text(entry.description),
                // Never stored on the tool config, so never read back off one.
                test_value: "",
            };
        });
    }

    function blank() {
        return {
            param: "",
            type: "text",
            required: true,
            description: "",
            test_value: "",
        };
    }

    // ----------------------------------------------------------------------
    // Controls — createElement only
    // ----------------------------------------------------------------------

    function input(value, placeholder, extra, onInput) {
        var node = element("input", "form-control form-control-sm " + (extra || ""));
        node.type = "text";
        node.value = value || "";
        node.placeholder = placeholder;
        node.addEventListener("input", function () {
            onInput(node.value.trim());
        });
        return node;
    }

    function select(pairs, selected, onChange) {
        var node = element("select", "form-select form-select-sm");

        pairs.forEach(function (pair) {
            var choice = document.createElement("option");
            choice.value = pair[0];
            choice.textContent = pair[1];
            if (pair[0] === selected) choice.selected = true;
            node.appendChild(choice);
        });

        node.addEventListener("change", function () {
            onChange(node.value);
        });

        return node;
    }

    function check(caption, checked, onChange) {
        var wrapper = element("div", "form-check mb-0");
        var box = element("input", "form-check-input");
        var text_ = element("label", "form-check-label small");

        box.type = "checkbox";
        box.checked = checked;
        box.addEventListener("change", function () {
            onChange(box.checked);
        });

        text_.textContent = caption;

        wrapper.appendChild(box);
        wrapper.appendChild(text_);

        return wrapper;
    }

    function remove(onClick) {
        var button = element("button", "btn btn-sm btn-outline-danger");
        button.type = "button";
        button.textContent = "×";
        button.title = "Remove this value";
        button.addEventListener("click", onClick);
        return button;
    }

    function label(caption) {
        var node = element("div", "col-md-auto text-muted small");
        node.textContent = caption;
        return node;
    }

    function note(message) {
        var node = element("p", "text-muted small mb-0");
        node.textContent = message;
        return node;
    }

    function cell(className, child) {
        var node = element("div", className);
        node.appendChild(child);
        return node;
    }

    function element(tag, className) {
        var node = document.createElement(tag);
        if (className) node.className = className.trim();
        return node;
    }

    function text(value) {
        return value === null || value === undefined ? "" : String(value);
    }
})();
