/**
 * Chatbot Configuration page — the "AI & Prompt" and "Actions" tabs.
 *
 * Repeating inputs (prompt variables, action parameters, action headers) are
 * edited as rows here and submitted as a single hidden JSON field each, so the
 * server has exactly one place to parse and validate their shape instead of
 * reassembling parallel form arrays.
 *
 * Row markup is built with createElement/textContent rather than innerHTML —
 * saved values (a variable value, a header token) are user data and must never
 * be re-parsed as markup.
 */
(function () {
    "use strict";

    var ACTION_ENDPOINTS = readJsonScript("caiActionEndpoints", { create: "", base: "" });
    var PARAM_TYPES = readJsonScript("caiParameterTypes", ["string", "number", "boolean"]);

    /** @param {string} id @param {*} fallback @returns {*} */
    function readJsonScript(id, fallback) {
        var el = document.getElementById(id);
        if (!el) return fallback;
        try {
            return JSON.parse(el.textContent);
        } catch (e) {
            return fallback;
        }
    }

    /**
     * Build a labelled input inside a flex column.
     * @param {object} opts
     * @returns {HTMLElement}
     */
    function inputCol(opts) {
        var wrap = document.createElement("div");
        wrap.className = opts.className || "flex-grow-1";

        var input = document.createElement("input");
        input.type = "text";
        input.className = "form-control form-control-sm" + (opts.monospace ? " font-monospace" : "");
        input.placeholder = opts.placeholder || "";
        input.value = opts.value || "";
        if (opts.dataField) input.setAttribute("data-field", opts.dataField);
        if (opts.pattern) input.pattern = opts.pattern;
        if (opts.maxlength) input.maxLength = opts.maxlength;
        if (opts.required) input.required = true;
        if (opts.ariaLabel) input.setAttribute("aria-label", opts.ariaLabel);

        wrap.appendChild(input);
        return wrap;
    }

    /** @param {HTMLElement} row @returns {HTMLElement} */
    function removeButton(row) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm btn-outline-danger flex-shrink-0";
        btn.title = "Remove";
        btn.innerHTML = '<i class="las la-times"></i>';
        btn.addEventListener("click", function () {
            row.remove();
        });
        return btn;
    }

    /** @param {string} selector @returns {Array<HTMLElement>} */
    function rowsIn(containerId) {
        var container = document.getElementById(containerId);
        return container ? Array.prototype.slice.call(container.children) : [];
    }

    /** @param {HTMLElement} row @param {string} field @returns {string} */
    function fieldValue(row, field) {
        var el = row.querySelector('[data-field="' + field + '"]');
        return el ? el.value.trim() : "";
    }

    // ----------------------------------------------------------------------
    // AI & Prompt tab — prompt variables
    // ----------------------------------------------------------------------

    /**
     * Append one variable row.
     * @param {string} name
     * @param {string} value
     */
    function addVariableRow(name, value) {
        var container = document.getElementById("caiVariableRows");
        if (!container) return;

        var row = document.createElement("div");
        row.className = "d-flex gap-2 align-items-start";

        row.appendChild(inputCol({
            className: "flex-grow-0", dataField: "name", value: name || "",
            placeholder: "COMPANY", monospace: true, pattern: "[A-Z][A-Z0-9_]{0,49}",
            maxlength: 50, ariaLabel: "Variable name",
        }));
        row.appendChild(inputCol({
            dataField: "value", value: value || "", placeholder: "Acme Inc",
            maxlength: 500, ariaLabel: "Variable value",
        }));
        row.appendChild(removeButton(row));

        container.appendChild(row);
    }

    /** Serialise variable rows into the hidden JSON field. */
    function collectVariables() {
        var out = [];
        rowsIn("caiVariableRows").forEach(function (row) {
            var name = fieldValue(row, "name").toUpperCase();
            if (!name) return;
            out.push({ name: name, value: fieldValue(row, "value") });
        });
        var hidden = document.getElementById("caiVariablesJson");
        if (hidden) hidden.value = JSON.stringify(out);
    }

    /** Show the API-key picker only when the chatbot uses a saved key. */
    function toggleLlmKeyField() {
        var checked = document.querySelector('input[name="llm_mode"]:checked');
        var field = document.getElementById("caiLlmKeyField");
        if (!checked || !field) return;
        field.style.display = checked.value === "api_key" ? "" : "none";
    }

    // ----------------------------------------------------------------------
    // Actions tab — parameter and header rows
    // ----------------------------------------------------------------------

    /** @param {object} param */
    function addParamRow(param) {
        var container = document.getElementById("caiParamRows");
        if (!container) return;
        param = param || {};

        var row = document.createElement("div");
        row.className = "d-flex gap-2 align-items-start";

        row.appendChild(inputCol({
            className: "flex-grow-0", dataField: "name", value: param.name || "",
            placeholder: "order_id", monospace: true, pattern: "[a-z][a-z0-9_]{0,49}",
            maxlength: 50, ariaLabel: "Parameter name",
        }));

        var typeWrap = document.createElement("div");
        typeWrap.className = "flex-grow-0";
        var select = document.createElement("select");
        select.className = "form-select form-select-sm";
        select.setAttribute("data-field", "type");
        select.setAttribute("aria-label", "Parameter type");
        PARAM_TYPES.forEach(function (type) {
            var option = document.createElement("option");
            option.value = type;
            option.textContent = type;
            if (type === (param.type || "string")) option.selected = true;
            select.appendChild(option);
        });
        typeWrap.appendChild(select);
        row.appendChild(typeWrap);

        row.appendChild(inputCol({
            dataField: "description", value: param.description || "",
            placeholder: "The visitor's order number", maxlength: 200,
            ariaLabel: "Parameter description",
        }));

        var checkWrap = document.createElement("div");
        checkWrap.className = "form-check mt-1 flex-shrink-0";
        var check = document.createElement("input");
        check.type = "checkbox";
        check.className = "form-check-input";
        check.setAttribute("data-field", "required");
        check.checked = !!param.required;
        check.id = "caiParamRequired-" + container.children.length;
        var checkLabel = document.createElement("label");
        checkLabel.className = "form-check-label small";
        checkLabel.setAttribute("for", check.id);
        checkLabel.textContent = "Required";
        checkWrap.appendChild(check);
        checkWrap.appendChild(checkLabel);
        row.appendChild(checkWrap);

        row.appendChild(removeButton(row));
        container.appendChild(row);
    }

    /** @param {object} header */
    function addHeaderRow(header) {
        var container = document.getElementById("caiHeaderRows");
        if (!container) return;
        header = header || {};

        var row = document.createElement("div");
        row.className = "d-flex gap-2 align-items-start";

        row.appendChild(inputCol({
            className: "flex-grow-0", dataField: "key", value: header.key || "",
            placeholder: "Authorization", monospace: true, pattern: "[A-Za-z0-9-]{1,100}",
            maxlength: 100, ariaLabel: "Header name",
        }));
        row.appendChild(inputCol({
            dataField: "value", value: header.value || "",
            placeholder: "Bearer ...", monospace: true, maxlength: 500,
            ariaLabel: "Header value",
        }));
        row.appendChild(removeButton(row));

        container.appendChild(row);
    }

    /** Serialise the action offcanvas' repeating rows into hidden JSON fields. */
    function collectActionRows() {
        var params = [];
        rowsIn("caiParamRows").forEach(function (row) {
            var name = fieldValue(row, "name").toLowerCase();
            if (!name) return;
            var typeEl = row.querySelector('[data-field="type"]');
            var requiredEl = row.querySelector('[data-field="required"]');
            params.push({
                name: name,
                type: typeEl ? typeEl.value : "string",
                description: fieldValue(row, "description"),
                required: !!(requiredEl && requiredEl.checked),
            });
        });

        var headers = [];
        rowsIn("caiHeaderRows").forEach(function (row) {
            var key = fieldValue(row, "key");
            if (!key) return;
            headers.push({ key: key, value: fieldValue(row, "value") });
        });

        document.getElementById("caiParametersJson").value = JSON.stringify(params);
        document.getElementById("caiHeadersJson").value = JSON.stringify(headers);
    }

    /** Put the action offcanvas back into "create" mode. */
    function resetActionForm() {
        var form = document.getElementById("chatbotActionForm");
        if (!form) return;

        form.reset();
        form.setAttribute("hx-post", ACTION_ENDPOINTS.create);
        document.getElementById("chatbotActionOffcanvasTitleText").textContent = "Add Action";
        document.getElementById("caiParamRows").innerHTML = "";
        document.getElementById("caiHeaderRows").innerHTML = "";
        document.getElementById("chatbotActionFormResponse").innerHTML = "";

        if (window.htmx) htmx.process(form);
    }

    /**
     * Load one existing action into the offcanvas for editing.
     * @param {DOMStringMap} data dataset of the clicked Edit button
     */
    function openEditActionForm(data) {
        var form = document.getElementById("chatbotActionForm");
        if (!form) return;

        resetActionForm();
        form.setAttribute("hx-post", ACTION_ENDPOINTS.base + data.actionId + "/update");
        document.getElementById("chatbotActionOffcanvasTitleText").textContent = "Edit Action";

        document.getElementById("caiActionName").value = data.actionName || "";
        document.getElementById("caiActionDescription").value = data.actionDescription || "";
        document.getElementById("caiActionMethod").value = data.actionMethod || "GET";
        document.getElementById("caiActionUrl").value = data.actionUrl || "";
        document.getElementById("caiActionBody").value = data.actionBody || "";
        document.getElementById("caiActionTimeout").value = data.actionTimeout || 10;

        parseList(data.actionParameters).forEach(addParamRow);
        parseList(data.actionHeaders).forEach(addHeaderRow);

        if (window.htmx) htmx.process(form);
        bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("chatbotActionOffcanvas")).show();
    }

    /** @param {string|undefined} raw @returns {Array} */
    function parseList(raw) {
        if (!raw) return [];
        try {
            var parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed : [];
        } catch (e) {
            return [];
        }
    }

    // ----------------------------------------------------------------------
    // Wiring
    // ----------------------------------------------------------------------

    // Called from the templates' inline onsubmit/onclick handlers.
    window.caiAddVariableRow = addVariableRow;
    window.caiAddParamRow = addParamRow;
    window.caiAddHeaderRow = addHeaderRow;
    window.caiToggleLlmKeyField = toggleLlmKeyField;
    window.caiResetActionForm = resetActionForm;

    window.caiOnSubmit = function () {
        collectVariables();
        return true;
    };

    window.caiOnActionSubmit = function () {
        collectActionRows();
        return true;
    };

    document.addEventListener("DOMContentLoaded", function () {
        readJsonScript("caiVariablesData", []).forEach(function (variable) {
            addVariableRow(variable.name, variable.value);
        });
        toggleLlmKeyField();

        // Row buttons are delegated because the actions table body is replaced
        // wholesale by HTMX out-of-band swaps after every save.
        document.addEventListener("click", function (event) {
            var editBtn = event.target.closest("[data-action-edit]");
            if (editBtn) {
                openEditActionForm(editBtn.dataset);
                return;
            }
            if (event.target.closest("[data-action-new]")) {
                resetActionForm();
            }
        });
    });
})();
