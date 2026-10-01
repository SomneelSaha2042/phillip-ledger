FROM python:3.12-slim
WORKDIR /app
ENV LEDGER_DB_PATH=/data/ledger.db
COPY app.py .
CMD ["python", "-u", "app.py", "serve", "--host", "0.0.0.0"]
