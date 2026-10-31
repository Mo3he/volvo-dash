FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/volvodash ./volvodash
RUN pip install --no-cache-dir .
COPY --from=web /web/dist ./static
ENV VOLVODASH_STATIC=/app/static VOLVODASH_DATA=/data PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "volvodash"]
