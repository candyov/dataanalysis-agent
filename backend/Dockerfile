FROM python:3.12-slim

WORKDIR /app

ENV PYTHONPATH=/app/src

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY .env /app/.env

RUN mkdir -p storage/uploads storage/output storage/audit

EXPOSE 8010

CMD ["uvicorn", "dia.main:app", "--host", "0.0.0.0", "--port", "8010"]
