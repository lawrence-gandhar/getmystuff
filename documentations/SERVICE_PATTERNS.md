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
