# A container, not a systemd unit plus a venv, for one reason: the three
# guarantees this deployment rests on — the artifact is read-only, the process
# cannot write its own directory, and configuration comes only from the
# environment — are declared HERE, in a file that is reviewed with the code and
# travels with it, where a unit file plus a venv leaves them as machine state
# nobody reads again after the day it was set up.
#
# Nothing in this image knows or cares where it runs.

FROM python:3.11-slim AS base

# ---------------------------------------------------------------- build deps
FROM base AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Only what the PUBLIC service needs. Deliberately not requirements.txt: that
# pulls streamlit, torch and transformers for the internal tool, none of which
# the public app imports — a public image should not carry code it cannot run.
COPY requirements-public.txt .
RUN pip install --no-cache-dir -r requirements-public.txt

# ---------------------------------------------------------------- runtime
FROM base AS runtime

# A real user, created here rather than left to the platform. UID is pinned so a
# host bind-mount's ownership is predictable.
RUN groupadd --gid 10001 medrag \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin medrag

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Only what the public service reads. `app.py`, `pages/` and `scripts/` are
# deliberately NOT copied: the internal tool is not merely unreachable over HTTP,
# it is not in the image at all. `medrag/` is needed because the public app reads
# its stores and renderers.
COPY --chown=root:root medrag/ /app/medrag/
COPY --chown=root:root public/ /app/public/
COPY --chown=root:root config/ /app/config/
COPY --chown=root:root docs/TERMS-DRAFT.md /app/docs/TERMS-DRAFT.md

# root owns the code, medrag runs it: the process cannot rewrite its own source.
# Combined with `read_only: true` on the container, nothing anywhere is writable
# except an explicit tmpfs.
USER 10001:10001

# The artifact mounts here, read-only. The image ships WITHOUT data — a 1.9 GB
# layer would have to be rebuilt and re-pushed for every refresh, and would make
# the image itself the thing that goes stale.
ENV PUBLIC_ARTIFACT_DIR=/data \
    MEDRAG_READ_ONLY=1 \
    MEDRAG_DATA_DIR=/data \
    PUBLIC_MAX_SNAPSHOT_AGE_DAYS=90

EXPOSE 8000

# The startup verification in public/main runs at import, so a missing, corrupt
# or stale artifact fails HERE — the container exits with the reason on stderr
# and never binds the port. That is the intended behaviour: a restart loop with
# a clear log line, rather than a page that returns 200 and serves nothing.
#
# --no-access-log is belt: public/main silences the server's access loggers at
# import, because a flag can be forgotten and the promise cannot depend on it.
CMD ["uvicorn", "public.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", \
     "--workers", "2"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4); \
sys.exit(0 if r.status==200 else 1)"
