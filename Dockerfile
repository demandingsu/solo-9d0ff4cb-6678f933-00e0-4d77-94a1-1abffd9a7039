FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sample_data.csv ./sample_data.csv

ENV DB_PATH=/data/annotate.db \
    SEED_CSV=/app/sample_data.csv \
    SEED_ON_EMPTY=1 \
    CLAIM_TTL_SECONDS=60

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
