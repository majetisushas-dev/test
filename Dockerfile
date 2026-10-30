FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
COPY backend/requirements-sip.txt /app/requirements-sip.txt
RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/requirements-sip.txt
COPY backend/app /app/app
COPY web /app/web
RUN mkdir -p /data
ENV VAANI_DATABASE_URL=sqlite:////data/vaani.db
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

