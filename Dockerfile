FROM python:3.12.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# 터미널(docker compose exec)로 들어가도 git을 쓸 수 있도록 설치
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    # Windows 바인드 마운트는 파일 소유자가 달라 git이 거부하므로 /app을 신뢰
    && git config --system --add safe.directory /app

WORKDIR /app

# 의존성만 먼저 설치해 코드가 바뀌어도 이 레이어는 캐시를 재사용
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
