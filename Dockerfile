# GetMyStuff — application image.
#
# Python 3.12 rather than the 3.10 this project used to run locally: the Deep
# Agents module depends on `deepagents`, which requires >= 3.11 (it imports
# typing.Required). Everything else in requirements.txt has cp312 wheels, so no
# compiler toolchain is needed in the final image.
FROM python:3.12-slim

# - PYTHONDONTWRITEBYTECODE: the source tree is bind-mounted in development, and
#   stale .pyc files next to edited sources cause confusing reload behaviour.
# - PYTHONUNBUFFERED: uvicorn/app logs reach `docker compose logs` immediately
#   instead of sitting in a pipe buffer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libpq5 is the runtime library psycopg2-binary links against. curl is used by
# the compose healthcheck. Nothing else is installed — every other dependency
# ships a wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies are copied and installed before the application code so that
# editing a .py file does not invalidate the (slow) pip layer.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Test dependencies live in a separate file but are installed into the same
# image: the suite runs via `docker compose exec app pytest`, because the local
# 3.10 venv cannot import app/services/deep_agents/ (needs >= 3.11). Kept in its
# own layer so editing it does not rebuild the main dependency layer.
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt

COPY . .

# Uploaded files land here (app/services/flow_builder/knowledge_base_service.py).
# Declared as a volume in docker-compose.yml so uploads survive a rebuild.
RUN mkdir -p /app/uploads

EXPOSE 8003

# main.py's __main__ block enables uvicorn --reload, which is right for
# development but should not be the image's default. The command is overridden
# in docker-compose.yml for the development workflow.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
