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

routes/
services/
models/
utils/
templates/
static/
```

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
