FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OBSERVER_BIND=0.0.0.0
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py db.py auth.py admin_cli.py root.py validation.py agent.py run.py uimodules.py ./
COPY static ./static
COPY templates ./templates
COPY fonts ./fonts

RUN useradd --system --no-create-home observer \
    && mkdir -p /app/files_buffer && chown observer /app/files_buffer
USER observer

# python run.py {remote|listener|dash|live|compute} PORT
ENTRYPOINT ["python", "run.py"]
