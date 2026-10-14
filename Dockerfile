FROM python:3.12-slim

RUN apt-get update \
	&& apt-get install -y --no-install-recommends git openssh-client ca-certificates \
	&& rm -rf /var/lib/apt/lists/* \
	&& useradd --create-home --uid 10001 ghis

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /build

USER ghis
WORKDIR /home/ghis
# GHIS_HOME on /tmp keeps the image working with a read-only root filesystem.
ENV PYTHONUNBUFFERED=1 \
	GHIS_HOST=0.0.0.0 \
	GHIS_HOME=/tmp/ghis \
	PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s \
	CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz', timeout=2)"

ENTRYPOINT ["ghis"]
CMD ["serve"]
