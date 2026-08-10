"""Tests for the data artifact: build, verify, and refuse to start.

The property that matters is the refusal. A public page serving an empty, wrong
or stale database while returning HTTP 200 is the same failure class as
everything else this codebase guards against — it looks healthy and is silently
answering from something other than what it claims — except that on a public
site nobody will tell you.

So each refusal is tested by constructing the broken artifact and asserting the
service will not start on it, and each is paired with an anti-vacuity check that
a GOOD artifact does start. "It refuses" proves nothing if it refuses everything.

No network: tests/netguard.py blocks sockets.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import netguard  # noqa: E402

netguard.install()

import pytest  # noqa: E402

from medrag.trials.client import TrialRecord  # noqa: E402
from medrag.trials.store import TrialStore  # noqa: E402
from public.artifact import (  # noqa: E402
    ArtifactError,
    read_embedded_snapshot_date,
    verify,
)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def _build_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_artifact", REPO / "scripts" / "build_artifact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(n: int = 5) -> Path:
    """A source directory shaped like `data/raw`."""
    root = Path(tempfile.mkdtemp()) / "raw"
    root.mkdir(parents=True)
    store = TrialStore(root / "trials.db")
    store.upsert(
        [TrialRecord(nct_id=f"NCT{i:06d}", brief_title=f"Trial {i}",
                     overall_status="RECRUITING", conditions=["Colorectal Cancer"],
                     eligibility_criteria="Inclusion Criteria:\n* MSS")
         for i in range(1, n + 1)], set_key="colorectal")
    store.close()
    return root


def _artifact(n: int = 5) -> Path:
    out = Path(tempfile.mkdtemp()) / "artifact"
    _build_module().build(_source(n), out, quiet=True)
    return out


# ------------------------------------------------------------------ build


def test_the_artifact_carries_its_snapshot_date_inside_the_database():
    """The requirement a filename cannot satisfy. Anyone can `mv` a file; the
    embedded table travels with the bytes."""
    out = _artifact()
    embedded = read_embedded_snapshot_date(out / "trials.db")
    assert embedded and embedded != "unknown"

    # Renaming the file changes nothing about what the app believes.
    renamed = out / "something-else-entirely.db"
    (out / "trials.db").rename(renamed)
    assert read_embedded_snapshot_date(renamed) == embedded


def test_the_artifact_publishes_checksums_that_match_what_was_written():
    out = _artifact()
    manifest = json.loads((out / "manifest.json").read_text())
    sums = (out / "SHA256SUMS").read_text()

    import hashlib

    for entry in manifest["files"]:
        digest = hashlib.sha256((out / entry["file"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"{entry['file']} does not match its manifest"
        assert f"{digest}  {entry['file']}" in sums


def test_two_builds_of_the_same_source_are_byte_identical():
    """The reproducibility claim, proven rather than asserted. `VACUUM INTO` is
    deterministic and the metadata written inside each database is derived from
    the DATA, never the wall clock — the build time lives in the manifest
    precisely so it cannot make the databases differ."""
    source = _source(20)
    module = _build_module()
    first = module.build(source, Path(tempfile.mkdtemp()) / "a", quiet=True)
    second = module.build(source, Path(tempfile.mkdtemp()) / "b", quiet=True)

    assert [f["sha256"] for f in first["files"]] == [f["sha256"] for f in second["files"]]
    # And the manifests differ ONLY in the build timestamp.
    a, b = dict(first), dict(second)
    a.pop("built_at"), b.pop("built_at")
    assert a == b


def test_building_does_not_modify_the_source_stores():
    """`VACUUM INTO` reads; it must never touch the stores it copies, so this is
    safe to run while the tool is in use."""
    source = _source(8)
    before = (source / "trials.db").read_bytes()
    _build_module().build(source, Path(tempfile.mkdtemp()) / "out", quiet=True)
    assert (source / "trials.db").read_bytes() == before


# ------------------------------------------------------- refuse to start


def test_a_good_artifact_verifies():
    """Anti-vacuity for everything below: if nothing verified, "it refuses"
    would be a statement about the checker, not about the artifact."""
    status = verify(_artifact())
    assert status.artifact_version >= 1
    assert status.age_days is not None and not status.stale
    assert status.total_bytes > 0


def test_a_missing_artifact_refuses_with_the_command_that_builds_one():
    with pytest.raises(ArtifactError) as exc:
        verify(Path(tempfile.mkdtemp()) / "absent")
    assert "build_artifact.py" in str(exc.value)


def test_a_directory_with_no_manifest_refuses():
    """A hand-copied database carries no checksums and no snapshot date, so
    nothing about it can be verified — which is exactly the deployment this
    check exists to stop."""
    out = _artifact()
    (out / "manifest.json").unlink()
    with pytest.raises(ArtifactError) as exc:
        verify(out)
    assert "not produced by scripts/build_artifact.py" in str(exc.value)


def test_a_corrupted_database_refuses_on_its_checksum():
    out = _artifact()
    target = out / "trials.db"
    blob = bytearray(target.read_bytes())
    blob[len(blob) // 2] ^= 0xFF          # one flipped bit, same length
    target.write_bytes(bytes(blob))

    with pytest.raises(ArtifactError) as exc:
        verify(out)
    message = str(exc.value)
    assert "does not match its published checksum" in message
    assert "Do not serve it" in message


def test_a_truncated_copy_refuses_on_size_before_hashing():
    """The most common way a deployment ends up serving half a database. Caught
    on size, so a partial 1.9 GB copy fails in milliseconds rather than after
    hashing what did arrive."""
    out = _artifact()
    target = out / "trials.db"
    blob = target.read_bytes()
    target.write_bytes(blob[: len(blob) // 2])

    with pytest.raises(ArtifactError) as exc:
        verify(out)
    assert "truncated or partial copy" in str(exc.value)


def test_a_missing_required_file_refuses():
    out = _artifact()
    (out / "trials.db").unlink()
    with pytest.raises(ArtifactError) as exc:
        verify(out)
    assert "did not complete" in str(exc.value)


def test_a_stale_snapshot_refuses_rather_than_serving_quietly_older_data():
    """Staleness is a startup refusal, not a warning: the alternative is a page
    that gets quietly more wrong every day nobody redeploys it, with a date in
    the masthead that nobody reads twice."""
    out = _artifact()
    future = datetime.now(timezone.utc) + timedelta(days=200)

    with pytest.raises(ArtifactError) as exc:
        verify(out, max_age_days=90, now=future)
    message = str(exc.value)
    assert "days old" in message and "at most 90" in message
    assert "PUBLIC_MAX_SNAPSHOT_AGE_DAYS" in message, \
        "the refusal must name the deliberate override"

    # The same artifact passes under a threshold that accepts its age, so the
    # refusal is about the age rather than about the artifact.
    assert verify(out, max_age_days=365, now=future).stale is False


def test_the_embedded_date_outranks_the_manifest():
    """The manifest sits beside the file and can be edited; the table travels
    inside it. Editing the manifest to claim a fresh snapshot must not work."""
    out = _artifact()
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["snapshot_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for entry in manifest["files"]:
        entry["snapshot_date"] = manifest["snapshot_date"]
    (out / "manifest.json").write_text(json.dumps(manifest))

    # Age the EMBEDDED date instead, and the artifact must be judged on it.
    conn = sqlite3.connect(str(out / "trials.db"))
    with conn:
        conn.execute("UPDATE snapshot_meta SET value = '2020-01-01 00:00:00' "
                     "WHERE key = 'snapshot_date'")
    conn.close()
    # The checksum no longer matches after that edit, so verify without it to
    # isolate the property under test.
    with pytest.raises(ArtifactError) as exc:
        verify(out, max_age_days=90, check_checksums=False)
    assert "days old" in str(exc.value)


def test_a_database_without_embedded_metadata_refuses():
    """A file someone produced by hand rather than through the builder."""
    out = _artifact()
    conn = sqlite3.connect(str(out / "trials.db"))
    with conn:
        conn.execute("DROP TABLE snapshot_meta")
    conn.close()
    with pytest.raises(ArtifactError) as exc:
        verify(out, check_checksums=False)
    assert "no embedded snapshot_meta" in str(exc.value)


def test_skipping_checksums_is_possible_but_reported_never_silent():
    """A named downgrade for slow storage. The health endpoint reports it, so an
    unverified start cannot be mistaken for a verified one."""
    out = _artifact()
    blob = bytearray((out / "trials.db").read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    (out / "trials.db").write_bytes(bytes(blob))

    with pytest.raises(ArtifactError):
        verify(out)                                    # checked: refuses
    assert verify(out, check_checksums=False)          # skipped: proceeds

    from public.config import PublicConfig

    assert PublicConfig().verify_checksums is True, "verification must default ON"


# ----------------------------------------------------- the health endpoint


def test_the_health_endpoint_reports_enough_to_judge_the_deployment():
    """An operator should be able to tell from one request whether the
    deployment is honest: how old the data is, which terms are in force, and
    which provider — if any — receives submitted text."""
    pytest.importorskip("fastapi")
    import importlib
    import os

    from fastapi.testclient import TestClient

    out = _artifact()
    saved = {k: os.environ.get(k) for k in ("PUBLIC_ARTIFACT_DIR", "MEDRAG_DATA_DIR")}
    try:
        os.environ["PUBLIC_ARTIFACT_DIR"] = str(out)
        os.environ["MEDRAG_DATA_DIR"] = str(out)
        import public.main as main

        importlib.reload(main)
        payload = TestClient(main.app).get("/healthz").json()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    for key in ("snapshot_date", "snapshot_age_days", "max_snapshot_age_days",
                "snapshot_stale", "terms_version", "model_provider",
                "artifact_verified", "checksums_verified", "features"):
        assert key in payload, f"the health endpoint does not report {key}"

    assert payload["artifact_verified"] is True
    assert payload["snapshot_stale"] is False
    # And it leaks nothing: a provider is named, never a key.
    body = json.dumps(payload).lower()
    for secret in ("sk-", "api_key", "token", "password", "passphrase"):
        assert secret not in body


def test_the_health_endpoint_warns_when_no_verified_artifact_is_configured():
    """The development fallback must never look like a verified deployment."""
    pytest.importorskip("fastapi")
    import importlib
    import os

    from fastapi.testclient import TestClient

    saved = os.environ.get("PUBLIC_ARTIFACT_DIR")
    try:
        os.environ.pop("PUBLIC_ARTIFACT_DIR", None)
        import public.main as main

        importlib.reload(main)
        payload = TestClient(main.app).get("/healthz").json()
    finally:
        if saved is not None:
            os.environ["PUBLIC_ARTIFACT_DIR"] = saved

    assert payload["artifact_verified"] is False
    assert "warning" in payload
    assert "not for a public deployment" in payload["warning"]


# ------------------------------------------------------------ the handlers


def test_the_data_touching_handlers_are_not_coroutines():
    """A coroutine handler runs ON the event loop, so the blocking SQLite reads
    and the CPU-bound biomarker screen inside it stall every other request in
    that worker — including /healthz. Measured: a concurrency test against async
    handlers did not finish in ten minutes; after this change the health check
    answered in 1.4ms during a heavy search.

    Pinned by inspection, because the symptom (a slow site) looks like ordinary
    load rather than like a bug.
    """
    import inspect

    import public.main as main

    for name in ("landscape", "landscape_pdf", "landscape_form", "index",
                 "terms_page", "memo", "claims"):
        fn = getattr(main, name)
        assert not inspect.iscoroutinefunction(fn), (
            f"{name} is `async def`; it does blocking work and will stall the "
            "event loop for every other request in the worker")


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except Exception:
                failures += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
