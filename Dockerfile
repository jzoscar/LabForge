FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client iputils-ping ipmitool docker.io \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY labforge ./labforge
RUN pip install --no-cache-dir '.[windows]'
COPY infra ./infra
ENV LABFORGE_DB_URL=sqlite:////data/labforge.db LABFORGE_IAC_PATH=/app/infra LABFORGE_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["labforge", "serve", "--host", "0.0.0.0", "--port", "8000"]
