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

## Error responses must be opted back into the swap

HTMX only swaps `2xx` responses. A route that answers `400` / `409` / `422` / `500` with a
human-readable alert would have that alert **silently discarded** — the user clicks the button
and nothing at all happens. `templates/base/layout.htm` installs one global `htmx:beforeSwap`
handler that re-enables the swap for error responses, so every route gets this for free:

```
document.addEventListener('htmx:beforeSwap', function (event) {
    var xhr = event.detail.xhr;
    if (xhr.status < 400) return;
    if (xhr.status === 401) return;          // handled by the login redirect instead

    var isHtml = (xhr.getResponseHeader('Content-Type') || '').includes('text/html');
    if (!isHtml || !xhr.responseText.trim()) {
        event.detail.serverResponse = '<div class="alert alert-danger">...</div>';
    }
    event.detail.shouldSwap = true;
    event.detail.isError = false;            // swap into the element's own hx-target
});
```

Two guards matter. `401` is left alone so the session-expiry redirect still wins and the login
page is never swapped into a partial. A non-HTML body (raw JSON from an unhandled exception) is
replaced with a generic sentence, so a payload or stack trace never reaches the user.

The consequence for route authors: **return the real status code**, not `200`, and return HTML.
The message will display.

---

# Offcanvas Panels Close Only on the Close Button

Every offcanvas in the app stays open until the user clicks its own close / cancel / save
button. A backdrop click and the `Esc` key are both inert — these panels hold configuration
forms, and losing a half-filled form to a stray click is not an acceptable failure mode.

Nothing is required of a new panel. `templates/base/layout.htm` locks this globally, right
after the Bootstrap bundle loads, because Bootstrap resolves the options per instance and a
panel can be created either way:

```
bootstrap.Offcanvas.Default.backdrop = 'static';   // panels created from JS
bootstrap.Offcanvas.Default.keyboard = false;
// …plus data-bs-backdrop="static" / data-bs-keyboard="false" stamped onto every
// .offcanvas element, since data attributes outrank the defaults for data-bs-toggle panels.
```

`backdrop: 'static'` makes a backdrop click fire `hidePrevented.bs.offcanvas` instead of
hiding; `keyboard: false` does the same for `Esc`. Panels swapped in by HTMX are stamped on
`htmx:load` / `htmx:afterSwap`, so a partial-delivered panel behaves identically.

Two rules for new panels:

* **Always give the panel a close control** — `data-bs-dismiss="offcanvas"` or an explicit
  `bootstrap.Offcanvas.getInstance(el).hide()`. With dismissal locked, a panel without one
  is a trap.
* **`data-bs-backdrop="false"` is still allowed** and is left alone by the lock. It means
  "no backdrop at all, keep the page behind usable" (the datasource configuration canvases
  dim the page with their own overlay instead). With no backdrop element there is nothing to
  click through, so the panel is already safe; only the keyboard lock is applied.

Programmatic closes are unaffected — a form that saves successfully and calls `.hide()` in
`hx-on::after-request` still closes itself.

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
