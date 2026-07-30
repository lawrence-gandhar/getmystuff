# SERVICE_PATTERNS.md

Standard service layer implementation pattern.

Services live under `app/services/<feature>/`, one subfolder per feature (e.g.
`app/services/datasource/datasource_service.py`). Callers import the specific function they
need by its full module path:

```
from app.services.datasource.datasource_service import create_datasource
```

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
