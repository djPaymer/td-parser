FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1

RUN pip install poetry

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false && poetry install --without dev --no-root

COPY app/ ./app/

ENTRYPOINT ["python", "-m", "app"]
CMD ["--help"]
