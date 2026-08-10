"""Verify the data artifact at startup, and refuse to serve if it is wrong.

A public page serving an empty or stale database while returning HTTP 200 is the
same failure class as everything else this codebase guards against: it looks
healthy and is silently answering from something other than what it claims. The
difference on a public site is that nobody will tell you — an analyst notices a
memo that reads thin, a stranger has no idea what the answer should have been.

So the service fails at STARTUP, not per request. Three refusals, each with the
command that fixes it:

  * **Missing.** The artifact directory, its manifest, or a required database is
    not there. Serving from an absent store would answer every question "nothing
    found", which reads as a finding rather than as an absence — the same
    not-assessed-vs-nothing-found rule, at the deployment layer.

  * **Checksum mismatch.** What is on the disk is not what was built. A truncated
    upload, a half-finished sync, or a swap someone did by hand. The bytes are
    checked against the manifest, so "the copy finished" is verified rather than
    assumed.

  * **Stale.** The snapshot is older than the threshold this deployment accepts.
    Read from INSIDE the database (`snapshot_meta`), never from the filename or
    the file's mtime — a filename is a label anyone can change with `mv`, and an
    mtime is changed by copying the file.

Staleness is a startup refusal rather than a warning because the alternative is
a page that quietly gets more wrong every day it is not redeployed, with a
snapshot date in the masthead that nobody reads twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "manifest.json"

#: How old a snapshot may be before the service refuses to start. Deliberately
#: generous relative to the registry's daily churn — this is a hard stop, not
#: the "please refresh" nudge, and the runbook's 30-day refresh cadence should
#: trip long before it.
DEFAULT_MAX_AGE_DAYS = 90


class ArtifactError(RuntimeError):
    """The artifact is missing, corrupt, or too old to serve.

    Carries a plain-language reason and a remedy, because the audience is an
    operator reading a failed container start, not a developer with a debugger.
    """


@dataclass(frozen=True)
class ArtifactStatus:
    """What is deployed, for the health endpoint. Every field is a fact read
    from the artifact itself rather than from configuration."""

    path: Path
    artifact_version: int
    snapshot_date: str
    age_days: int | None
    max_age_days: int
    total_bytes: int
    files: list[dict] = field(default_factory=list)
    built_at: str = ""

    @property
    def stale(self) -> bool:
        return self.age_days is not None and self.age_days > self.max_age_days

    @property
    def snapshot_display(self) -> str:
        parsed = _parse_date(self.snapshot_date)
        return parsed.strftime("%d %B %Y") if parsed else self.snapshot_date


def _parse_date(text: str) -> datetime | None:
    if not text or text == "unknown":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text).split(".")[0], fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_embedded_snapshot_date(db_path: Path) -> str:
    """The snapshot date from INSIDE the database.

    Opened read-only and never creating anything. Returns "" when the table is
    absent, which is itself a refusal condition — a database with no embedded
    metadata was not produced by `scripts/build_artifact.py`, and a copy someone
    made by hand is exactly what this check exists to catch.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return ""
    try:
        row = conn.execute(
            "SELECT value FROM snapshot_meta WHERE key = 'snapshot_date'").fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def verify(artifact_dir: Path, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
           check_checksums: bool = True, now: datetime | None = None) -> ArtifactStatus:
    """Verify and describe the artifact, or raise `ArtifactError`.

    `check_checksums=False` exists for a large artifact on a slow disk where an
    operator has already verified out of band — it is a deliberate, named
    downgrade rather than a silent skip, and the health endpoint reports that it
    was skipped so nobody mistakes an unverified start for a verified one.
    """
    artifact_dir = Path(artifact_dir)
    now = now or datetime.now(timezone.utc)

    if not artifact_dir.exists():
        raise ArtifactError(
            f"no data artifact at {artifact_dir}.\n"
            "  The service will not start without one: an absent store answers every\n"
            "  question 'nothing found', which reads as a finding rather than an absence.\n"
            "  Build one:   python scripts/build_artifact.py --out dist/artifact\n"
            "  Then point PUBLIC_ARTIFACT_DIR at it.")

    manifest_path = artifact_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise ArtifactError(
            f"{artifact_dir} has no {MANIFEST_NAME}.\n"
            "  This directory was not produced by scripts/build_artifact.py — a\n"
            "  hand-copied database carries no checksums and no snapshot date, so\n"
            "  nothing about it can be verified.\n"
            "  Rebuild:   python scripts/build_artifact.py --out dist/artifact")

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise ArtifactError(
            f"{manifest_path} could not be read as JSON ({exc}). The artifact is "
            "damaged; rebuild it.") from exc

    files = manifest.get("files") or []
    if not files:
        raise ArtifactError(f"{manifest_path} lists no files. Rebuild the artifact.")

    for entry in files:
        path = artifact_dir / entry["file"]
        if not path.exists():
            if entry.get("required"):
                raise ArtifactError(
                    f"{entry['file']} is listed in the manifest but missing from "
                    f"{artifact_dir}.\n  The upload or copy did not complete. Re-copy "
                    "the artifact and verify:\n"
                    f"    cd {artifact_dir} && shasum -a 256 -c SHA256SUMS")
            continue

        size = path.stat().st_size
        if size != entry["bytes"]:
            raise ArtifactError(
                f"{entry['file']} is {size:,} bytes but the manifest says "
                f"{entry['bytes']:,}.\n  This is a truncated or partial copy — the most "
                "common way a deployment ends up serving half a database.\n"
                f"  Re-copy and verify:  cd {artifact_dir} && shasum -a 256 -c SHA256SUMS")

        if check_checksums:
            digest = _sha256(path)
            if digest != entry["sha256"]:
                raise ArtifactError(
                    f"{entry['file']} does not match its published checksum.\n"
                    f"    expected {entry['sha256']}\n"
                    f"    found    {digest}\n"
                    "  What is deployed is not what was built. Do not serve it: re-copy "
                    "the artifact from the build output and verify before restarting.")

    required = [e for e in files if e.get("required")]
    for entry in required:
        embedded = read_embedded_snapshot_date(artifact_dir / entry["file"])
        if not embedded:
            raise ArtifactError(
                f"{entry['file']} carries no embedded snapshot_meta table.\n"
                "  Its age cannot be established from the file itself, and a filename "
                "is not evidence — anyone can rename it.\n"
                "  Rebuild:   python scripts/build_artifact.py --out dist/artifact")
        # The embedded value is authoritative over the manifest: the manifest
        # sits beside the file and can be edited, the table travels inside it.
        entry["snapshot_date"] = embedded

    snapshot_date = max((e.get("snapshot_date", "") for e in required),
                        default=manifest.get("snapshot_date", "unknown")) or "unknown"
    parsed = _parse_date(snapshot_date)
    age_days = (now - parsed).days if parsed else None

    status = ArtifactStatus(
        path=artifact_dir,
        artifact_version=int(manifest.get("artifact_version", 0)),
        snapshot_date=snapshot_date,
        age_days=age_days,
        max_age_days=max_age_days,
        total_bytes=int(manifest.get("total_bytes", 0)),
        files=files,
        built_at=manifest.get("built_at", ""),
    )

    if age_days is None:
        raise ArtifactError(
            f"the artifact's snapshot date ({snapshot_date!r}) could not be read as a "
            "date, so its age is unknown.\n  An unknown age cannot be checked against "
            "the staleness threshold, and serving data of unknown age on a public site "
            "is exactly what this check exists to stop.\n"
            "  Rebuild:   python scripts/build_artifact.py --out dist/artifact")

    if status.stale:
        raise ArtifactError(
            f"the data snapshot is {age_days} days old ({status.snapshot_display}) and "
            f"this deployment accepts at most {max_age_days}.\n"
            "  Refusing to start rather than serving stale data behind a healthy-looking\n"
            "  page. Build a fresh artifact and redeploy, or raise the threshold\n"
            "  deliberately with PUBLIC_MAX_SNAPSHOT_AGE_DAYS if you accept the age.")

    return status
