FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install poetry

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false && poetry install --without dev

COPY app/ ./app/

CMD ["uvicorn", "app.main:main_app", "--host", "0.0.0.0", "--port", "8000"]
