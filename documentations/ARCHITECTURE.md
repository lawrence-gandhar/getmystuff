# ARCHITECTURE.md

## System Architecture

GetMyStuff follows a **layered enterprise architecture**.

```
Client
  ↓
Routes
  ↓
Services
  ↓
DB Utils
  ↓
Database
```

---

# Project Structure

```
app/

db/          shared infra (base.py, db_sessions.py, db_utils.py, models.py) + per-feature subfolders
routes/      per-feature subfolders (auth/, dashboard/, datasource/, ai_settings/, ai_analytics/, chatbot/, flow_builder/)
services/    per-feature subfolders (datasource/, ai_settings/, ai_analytics/, chatbot/, flow_builder/, ai_inbuilt/)
models/      per-feature subfolders (user/, datasource/, ai_settings/, chatbot/, ai_analytics/, subscriptions/, flow_builder/, ai_inbuilt/)
schemas/
utils/
templates/
static/
```

`db/`, `models/`, `routes/`, and `services/` group related files by feature — a feature's
subfolder is named the same across all four layers it appears in (e.g. `datasource/` exists
under both `routes/` and `services/`). Each subfolder's `__init__.py` re-exports its public
symbols for `models/` and `routes/`; `services/` subfolders use plain empty `__init__.py`
files since service callers import specific functions by full module path. See `CLAUDE.md`
for the full rule and `flow_builder/`/`ai_inbuilt/` for the reference implementation.

---

# Responsibilities

## Routes

Routes handle:

* HTTP requests
* validation
* response rendering

Routes must NOT contain business logic.

Example:

```
@post("/datasource")
async def create_datasource(data):
    sanitized = sanitize(data)
    return datasource_service.create_datasource(sanitized)
```

---

# Services

Services handle:

* business rules
* validation
* orchestration
* database operations

Services never return HTML.

---

# Utils

Utils contain reusable helpers.

Examples:

* db_utils.py
* validators.py
* exceptions.py

---

# Templates

Templates must follow:

```
templates/
   base.html
   dashboard/
   datasource/
```

Each feature must have its own folder.

---

# Static Assets

```
static/css
static/js
static/img
```

Use Bootstrap 5.

Avoid inline styles where possible.
