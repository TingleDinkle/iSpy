FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tracker/ tracker/
COPY sql/ sql/
COPY migrations/ migrations/
COPY alembic.ini manage.py daily_snapshot.py detect_spikes.py detect_events.py \
     analyze_reviews.py notify.py market_report.py ./

CMD ["python", "daily_snapshot.py"]
