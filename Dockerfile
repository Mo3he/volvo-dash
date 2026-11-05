# The web app is plain static files, so build it once on the native platform.
FROM --platform=$BUILDPLATFORM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
LABEL org.opencontainers.image.description="Self-hosted trip and charging log for Volvo EVs"
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/volvodash ./volvodash
RUN pip install --no-cache-dir .
COPY --from=web /web/dist ./static
ENV VOLVODASH_STATIC=/app/static VOLVODASH_DATA=/data PORT=8000
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -m volvodash.health
CMD ["python", "-m", "volvodash"]
