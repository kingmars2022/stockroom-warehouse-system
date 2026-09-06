FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini start.sh ./
RUN chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
