FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY loop ./loop
COPY cloud_entry.py ./

RUN pip install --no-cache-dir .

# Spaces containers run as a non-root user; keep the SQLite graph somewhere writable.
ENV LOOP_DATABASE_PATH=/tmp/loop.db
ENV PORT=7860
EXPOSE 7860

CMD ["python", "cloud_entry.py"]
