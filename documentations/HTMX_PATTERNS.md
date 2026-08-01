# HTMX_PATTERNS.md

HTMX patterns used across the application.

---

# Create Form

```
<form
 hx-post="/datasource/create"
 hx-target="#datasource-table"
 hx-swap="outerHTML">

<input name="name" required>
<button type="submit">Create</button>

</form>
```

---

# Delete Row

```
<button
 hx-delete="/datasource/{{id}}"
 hx-target="#row-{{id}}"
 hx-swap="outerHTML">

Delete
</button>
```

---

# Table Reload

```
<div
 hx-get="/datasource/table"
 hx-trigger="load"
 hx-target="#table-body">
</div>
```

---

# Inline Edit

```
<input
 hx-post="/datasource/update"
 hx-trigger="blur">
```

---

# Cascading Select

One choice re-renders the field that depends on it, swapping the whole field so its options are
always real. `hx-include` sends the fields the server needs alongside the one that changed.

```
<select name="datasource_id"
 hx-get="/tool-configs/tables"
 hx-target="#toolTableField"
 hx-swap="outerHTML"
 hx-trigger="change">
```

---

# Resetting a Dependent Panel (out-of-band)

When a cascade invalidates more than its own field, the response carries the extra element with
`hx-swap-oob="true"` — one request, two swaps, no second round trip.

Changing the datasource replaces the Table field *and* resets the query builder, because a
query (its joins especially) belongs to one datasource. See
`templates/tool_configs/partials/table_field_response.htm`.

```
{# the swap target #}
{% include "tool_configs/partials/table_field.htm" %}

{# rides along, replacing #toolQueryBuilder by id #}
{% with oob_swap = True %}
{% include "tool_configs/partials/query_builder.htm" %}
{% endwith %}
```

---

# Carrying Conversation State (server-owned)

For a multi-turn panel, the server writes the state into the response and the form pulls it back
in with `hx-include`. The state is then whatever the server last confirmed — not whatever the
browser has been holding — and a failed turn can re-render it unchanged instead of discarding
it. See `templates/sql_assist/partials/result.htm`.

```
{# in the form #}
<form hx-post="/sql-assist/generate"
 hx-target="#sqlAssistResult"
 hx-include="#sqlAssistHistory">

{# in the response, single-quoted: tojson does not escape double quotes #}
<input type="hidden" id="sqlAssistHistory" name="history_json"
 value='{{ history | tojson }}'>
```

---

# Repeating Rows as One JSON Field

Builder rows (query columns, joins, action parameters) are serialised by JS into a single hidden
JSON field rather than parallel form arrays, so the server has exactly one place to parse and
validate their shape. Whatever is submitted is re-validated server-side regardless of what the
form displayed.

```
<textarea name="config_json" data-builder-json></textarea>
```

See `static/js/tool_configs.js`, `static/js/chatbot_ai_settings.js`.

---

# Errors Inside the Panel

A panel that covers the viewport must render its own errors — an alert swapped into the page
behind it is invisible. Mutations answer with a marker plus the rebuilt list; failures answer
with an alert into the same target, leaving everything the user typed untouched.

```
<div id="toolConfigFormResponse"></div>   {# banner lives inside the offcanvas #}
```

---

# Best Practices

Use HTMX for:

* table updates
* forms
* pagination
* search

Avoid full page reloads.

Build repeating-row markup with `createElement`, never `innerHTML` — table and column names come
from the user's own database and must never be re-parsed as markup.
