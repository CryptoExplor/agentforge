# Production image. Development conveniences live in Dockerfile.dev.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY server /app/server
COPY sdk /app/sdk
COPY protocol /app/protocol
COPY migrations /app/migrations
COPY alembic.ini /app/
COPY web /app/web
COPY AGENTFORGE_ARCHITECTURE.md ANTIGRAVITY_IMPLEMENTATION_BRIEF.md SECURITY.md /app/

RUN pip install --no-cache-dir -e '.[postgres]'

ENV AGENTFORGE_ENV=production
ENV AGENTFORGE_AUTO_CREATE_SCHEMA=false
ENV AGENTFORGE_ENABLE_MOCK_FAUCET=false
ENV AGENTFORGE_HOST=0.0.0.0
ENV AGENTFORGE_PORT=8080
EXPOSE 8080

# Schema changes are explicit migrations, never SQLAlchemy create_all.
CMD ["sh", "-c", "alembic upgrade head && uvicorn agentforge_server.app:app --app-dir server --host 0.0.0.0 --port 8080"]
