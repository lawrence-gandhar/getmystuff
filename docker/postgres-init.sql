-- Runs once, on first boot of an empty pgdata volume.
--
-- The in-built LLM's knowledge_chunks table stores embeddings in a `vector`
-- column (app/models/ai_inbuilt/models.py), so the extension has to exist
-- before the table is created. Alembic revision a3f5c9d21b47 also issues this
-- statement, but main.py's on_startup create_all path does not — and that is
-- what runs first on a fresh database.
CREATE EXTENSION IF NOT EXISTS vector;
