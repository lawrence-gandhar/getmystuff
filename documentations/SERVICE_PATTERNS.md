# SERVICE_PATTERNS.md

Standard service layer implementation pattern.

Services live under `app/services/<feature>/`, one subfolder per feature (e.g.
`app/services/datasource/datasource_service.py`). Callers import the specific function they
need by its full module path:

```
from app.services.datasource.datasource_service import create_datasource
```

**A new module always gets its own same-named folder, in every layer it needs** — even when
only one existing feature calls it. Being called from a feature is not a reason to live inside
it; the folder boundary tracks what the module *is*, not who calls it.

`sql_assist` is the reference case: the Ask AI panel is opened from the Tool Configs page, but
it lives in `app/services/sql_assist/` and `app/routes/sql_assist/` with its own
`templates/sql_assist/`, because generating SQL from a schema needs a datasource and nothing
else. See [SQL_ASSIST.md](SQL_ASSIST.md).

---

# Shared Rules Belong in `utils/`, Not in a Sibling Feature

When two features need the same rule, put the rule in `app/utils/` and have both import it.
Reaching into another feature's module for a constant or a validator couples them permanently,
and the coupling is invisible until one of them changes.

`app/utils/query_joins.py` is the reference case: the Tool Configs library and the
Configurations page both author joins, and both import the join rules from there. Neither
imports the other. See [QUERY_JOINS.md](QUERY_JOINS.md).

The same applies to shared input validation — `app/utils/validators.py` holds
`require_object_name`, `require_identifier`, `parse_optional_uuid` and friends, so the same
input is held to the same rule *and rejected with the same wording* everywhere.

---

# Example Service

```
# app/services/datasource/datasource_service.py

from app.utils.db_utils import insert_record
from app.utils.exceptions import ValidationError


class DatasourceService:

    @staticmethod
    def create_datasource(name: str, connection: str):

        if not name:
            raise ValidationError("Datasource name cannot be empty")

        payload = {
            "name": name,
            "connection": connection
        }

        insert_record("datasources", payload)

        return {
            "status": "success",
            "message": "Datasource created successfully"
        }
```

---

# Service Rules

Services must:

* validate inputs
* enforce business rules
* call db_utils
* return structured responses

Services must NOT:

* access HTTP requests
* render HTML

---

# Datasource Table/Column Status Cascade

The datasource preview exposes two switches per table — the table itself and each of
its columns — both stored in `DataSource.configuration_data`:

```
configuration_data = {
    "orders": {
        "status": "active",                 # the table switch
        "column_data": {
            "id": {"column_name": "id", "status": "active"},   # the column switches
        },
    },
}
```

The table switch **owns** its columns. `toggle_table_status_service()` cascades in
both directions: every column in `column_data` is written to the table's new status,
whether that status is `active` or `inactive`.

Why both directions and not just deactivation:

* An **active** table whose columns are all inactive contributes no data to a query,
  so leaving the columns alone on activation reads as the activation having silently
  done nothing.
* An **inactive** table with active columns is the mirror of the same disagreement.

The consequence is deliberate: re-activating a table discards the per-column choices
the user made before it was switched off. The table switch is the coarse control, and
it wins.

`toggle_column_status_service()` enforces the same ownership from the other side — it
refuses to activate a column while its table is inactive, with a 400 and a readable
message rather than a silent no-op.

A table discovered after the datasource was created has no `column_data` yet. The
cascade is a no-op there; the view route falls back to a live schema fetch and treats
every column as active, which matches the table's own default of `"active"` for
unconfigured tables.

---

# Who reads the status — `app/utils/datasource_status.py`

The switches above are only worth having if every feature honours them, so the reading
is done in one shared module rather than at each call site. It lives in `utils/`
alongside `query_joins.py` and `sql_guard.py` for the same reason those do: four
features depend on it, so it belongs to none of them.

```python
from app.utils.datasource_status import (
    is_table_active, is_column_active,
    active_table_names, active_column_names, active_columns_by_table,
    inactive_table_names, first_inactive_reference,
    NO_ACTIVE_TABLES_MESSAGE, inactive_table_message,
    no_active_columns_message, inactive_column_message,
)
```

Four rules, all of them load-bearing:

1. **Absent means active.** Only the literal `"inactive"` switches something off.
   A missing table entry, an empty `column_data`, a `configuration_data` of `None`,
   an unrecognised value — all active. Every datasource created before metadata
   collection worked has an empty `configuration_data`, so "active only if it says
   active" would empty every dropdown in the application for those users.
2. **Nothing raises.** `configuration_data` is user-written JSON and hand-editable, so
   an unrecognised shape reads as unconfigured rather than taking a page down. Callers
   raise — the services as an `HTTPException`, the executor as a `ToolQueryError` —
   which is why the message strings live in the module and not in either of them.
3. **The cascade is re-applied on read.** `active_column_names` returns `[]` for an
   inactive table whatever its `column_data` says. The write side cascades too, but a
   row edited straight in psql can disagree with itself.
4. **The caller owns the list of names.** Every function filters names the caller read
   from the live database and never returns one it was not given, so a column dropped
   from the real table but still recorded in `configuration_data` is never offered.

Where it is enforced, and what "enforced" means in each place:

| Surface | Behaviour |
|---|---|
| Data Sources listing (`search_sort_tables`) | **Opt-in filter, not a rule** — this is where the switches are set, so it must be able to show inactive tables. |
| Tool Config pickers (`get_table_choices`, `get_column_choices`, `get_column_map`) | Inactive tables and columns are **not offered at all**. All-inactive raises a named message rather than showing an empty dropdown. |
| Ask AI table picker (`sql_assist_service.get_table_choices`) | Same. |
| Ask AI schema (`sql_assist_service._load_metadata`) | Inactive tables refused by name; inactive columns, primary-key entries and foreign keys **pruned out of the metadata** before the prompt is built. The model is never shown a column it may not read. |
| Agent execution (`query_executor`) | The real guarantee. Checked on **every run**, because a tool config is a standing permission written once and run for months. Builder mode checks tables *and* columns; SQL mode checks the tables the tool records (`table_name` + `extra_tables`), because nothing parses the statement. |

Saving a tool config deliberately does **not** check the status — a datasource that is
momentarily unreachable must not make an existing config uneditable. Switching a column
off makes a config *unrunnable until fixed*, not unopenable.

### What happens to a saved config that names a switched-off column

It fails, loudly, with a message the agent relays: *"Column 'orders.total' is inactive
in this datasource…"*. It is not dropped from the query. A dropped filter widens the
result set, a dropped group-by changes what each row counts, and either way the query
still returns a number the agent states as fact. A tool that says it needs
reconfiguring is recoverable; a plausible wrong figure is not.
