# UNTESTED: Docker is not available on the build box. Written, never built or run.
FROM python:3.13.5-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --create-home --uid 10001 capstop
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml ./
COPY capstop ./capstop
RUN pip install --no-cache-dir --no-deps --no-build-isolation . && rm -rf /app/build
USER capstop
WORKDIR /data
EXPOSE 8787
# Binding 0.0.0.0 inside the container needs --allow-remote; publish only on loopback:
#   docker run --rm -p 127.0.0.1:8787:8787 -v capstop-data:/data capstop
ENTRYPOINT ["capstop", "serve", "--host", "0.0.0.0", "--allow-remote", "--db", "/data/capstop.db"]
CMD ["--upstream", "https://api.openai.com"]
