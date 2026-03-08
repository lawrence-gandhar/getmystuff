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

# Best Practices

Use HTMX for:

* table updates
* forms
* pagination
* search

Avoid full page reloads.
