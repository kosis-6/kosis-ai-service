# ---- 화면 빌드: frontend/ (React + Vite) → frontend/dist ----
FROM node:22-slim AS frontend
WORKDIR /frontend
# 의존성만 먼저 설치해 화면 코드가 바뀌어도 이 레이어는 캐시를 재사용
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 서버 ----
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
    # Windows 바인드 마운트는 파일 소유자가 달라 git이 거부하므로 /workspace를 신뢰
    && git config --system --add safe.directory /workspace

# 레포와 같은 배치: /workspace/backend, /workspace/frontend/dist
WORKDIR /workspace/backend

# 의존성만 먼저 설치해 코드가 바뀌어도 이 레이어는 캐시를 재사용
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project

COPY backend/ ./
# 화면은 빌드 결과물만 넣는다 (app/main.py가 /chat 으로 서빙)
COPY --from=frontend /frontend/dist ../frontend/dist

EXPOSE 8000

# 프록시(nginx, ALB)가 https를 받아 넘기면 X-Forwarded-Proto·For를 믿어야 서버가 https와 진짜 IP를 안다
# (Refresh Token 쿠키 Secure, HSTS, 로그인 시도 제한). 믿을 프록시 주소는 배포 환경 변수로 준다:
#   FORWARDED_ALLOW_IPS=프록시 주소 (uvicorn이 직접 읽는다. 기본 127.0.0.1)
# 이 컨테이너에 프록시를 거치지 않고 바로 들어올 수 없을 때만 '*'로 둔다
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
