# GetMyStuff

An enterprise-grade AI-powered analytics platform. Connect multiple data sources — RDBMS,
MongoDB, or uploaded files — and query them in natural language, with no dashboards, SQL, or
engineering expertise required.

## Core capabilities

- Multi-database connectivity (PostgreSQL, MySQL, MongoDB)
- Natural language querying and AI-driven analytics
- File ingestion pipelines (CSV, XLSX, JSON, Parquet, Avro)
- An embeddable chatbot widget backed by a visual conversation-flow builder
- An in-built local knowledge base (Ollama + pgvector) for AI-fallback grounding
- Enterprise-grade validation and error handling

## Tech stack

| Layer           | Technology                          |
|------------------|--------------------------------------|
| Backend          | Python 3.11+, Litestar               |
| Frontend         | HTMX, Bootstrap 5, HTML5, CSS3        |
| Databases        | PostgreSQL, MySQL, MongoDB            |
| Data processing  | Pandas, PyArrow                       |

## Project structure

`db/`, `models/`, `routes/`, and `services/` are organized into per-feature subfolders —
each feature (auth, dashboard, datasource, ai_settings, ai_analytics, chatbot, flow_builder,
ai_inbuilt, subscriptions) gets an identically-named subfolder in whichever of those four
layers it actually needs. Shared infrastructure (`db_utils.py`, `base.py`, `db_sessions.py`,
`models.py` — the model registry) stays at the top level of `db/`.

```
app/
├── db/            base.py, db_sessions.py, db_utils.py, models.py, auth/, flow_builder/, ai_inbuilt/
├── models/        user/, datasource/, ai_settings/, chatbot/, ai_analytics/, subscriptions/, flow_builder/, ai_inbuilt/
├── routes/        auth/, dashboard/, datasource/, ai_settings/, ai_analytics/, chatbot/, flow_builder/
├── services/       datasource/, ai_settings/, ai_analytics/, chatbot/, flow_builder/, ai_inbuilt/
├── schemas/        Pydantic DTOs
├── utils/          crypto, file handling, CSV/Parquet conversion
├── templates/       Jinja templates, grouped by feature
└── static/          css/, js/
```

See `CLAUDE.md` for the full architecture rules and conventions this codebase follows.

## Documentation

- [ARCHITECTURE.md](documentations/ARCHITECTURE.md) — layered architecture overview
- [ERROR_HANDLING.md](documentations/ERROR_HANDLING.md) — error handling philosophy and types
- [HTMX_PATTERNS.md](documentations/HTMX_PATTERNS.md) — HTMX usage patterns
- [SERVICE_PATTERNS.md](documentations/SERVICE_PATTERNS.md) — service layer implementation pattern
- [FLOW_BUILDER.md](documentations/FLOW_BUILDER.md) — visual conversation-flow builder for the chatbot widget
- [AI_INBUILT.md](documentations/AI_INBUILT.md) — in-built local Ollama + pgvector knowledge base pipeline

## Running locally

```
pip install -r requirements.txt
python main.py
```

The app serves on `http://0.0.0.0:8003`.
