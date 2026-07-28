"""Tests for encryption at rest, permissions, and offline enforcement.

Covers the properties that actually matter: ciphertext reveals no plaintext,
a wrong passphrase fails cleanly, tampering is detected rather than silently
deserialized, nonces never repeat, and offline mode cannot be bypassed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medrag import crypto  # noqa: E402
from medrag.chunking import chunk_document  # noqa: E402
from medrag.config import Config  # noqa: E402
from medrag.crypto import (  # noqa: E402
    CryptoError,
    decrypt_bytes,
    encrypt_bytes,
    is_encrypted,
    read_secure,
    write_secure,
)
from medrag.documents import Document  # noqa: E402
from medrag.embeddings import HashingEmbedder, get_embedder  # noqa: E402
from medrag.ingest import pubmed  # noqa: E402
from medrag.ingest.store import load_corpus, save_corpus  # noqa: E402
from medrag.vectorstore import VectorStore  # noqa: E402

PASSPHRASE = "correct horse battery staple"
SECRET = b"Patient presented with a hazard ratio of 0.79 and an unusual marker."

DOC = Document(
    doc_id="SAMPLE-001",
    title="Distinctive Title For Leak Detection",
    text=(
        "Background: A uniquely identifiable phrase appears here.\n\n"
        "Results: The effect estimate was 0.79 with a 95 percent confidence interval."
    ),
    authors=["A Author"],
    journal="J Test",
    year="2024",
)


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


# ------------------------------------------------------------- primitives


def test_roundtrip_recovers_plaintext():
    assert decrypt_bytes(encrypt_bytes(SECRET, PASSPHRASE), PASSPHRASE) == SECRET


def test_ciphertext_leaks_no_plaintext():
    blob = encrypt_bytes(SECRET, PASSPHRASE)
    assert SECRET not in blob
    assert b"hazard" not in blob and b"Patient" not in blob
    assert blob.startswith(crypto.MAGIC)


def test_wrong_passphrase_raises_not_returns_garbage():
    blob = encrypt_bytes(SECRET, PASSPHRASE)
    try:
        decrypt_bytes(blob, "not the passphrase")
    except CryptoError as exc:
        assert "wrong passphrase" in str(exc)
    else:
        raise AssertionError("a wrong passphrase must raise, never return plaintext")


def test_tampering_is_detected():
    blob = bytearray(encrypt_bytes(SECRET, PASSPHRASE))
    blob[-3] ^= 0x01  # flip one bit of ciphertext
    try:
        decrypt_bytes(bytes(blob), PASSPHRASE)
    except CryptoError:
        pass
    else:
        raise AssertionError("GCM must reject modified ciphertext")


def test_header_tampering_is_detected():
    """The salt is authenticated as associated data, so it cannot be swapped."""
    blob = bytearray(encrypt_bytes(SECRET, PASSPHRASE))
    blob[len(crypto.MAGIC) + 2] ^= 0xFF  # mutate a salt byte
    try:
        decrypt_bytes(bytes(blob), PASSPHRASE)
    except CryptoError:
        pass
    else:
        raise AssertionError("modified header must fail authentication")


def test_nonce_and_salt_are_unique_per_write():
    blobs = [encrypt_bytes(SECRET, PASSPHRASE) for _ in range(20)]
    header = len(crypto.MAGIC) + 1
    salts = {b[header : header + crypto.SALT_LEN] for b in blobs}
    nonces = {b[header + crypto.SALT_LEN : header + crypto.SALT_LEN + crypto.NONCE_LEN] for b in blobs}
    assert len(salts) == 20, "salt reuse would let one cracked key open many files"
    assert len(nonces) == 20, "GCM nonce reuse under one key is catastrophic"
    assert len(set(blobs)) == 20, "identical plaintext must not produce identical ciphertext"


def test_truncated_file_reports_clearly():
    try:
        decrypt_bytes(crypto.MAGIC + b"\x01short", PASSPHRASE)
    except CryptoError as exc:
        assert "truncated" in str(exc) or "version" in str(exc)
    else:
        raise AssertionError("truncated envelope must raise")


# ------------------------------------------------------------- file layer


def test_write_secure_sets_restrictive_permissions():
    path = _tmpdir() / "secret.bin"
    write_secure(path, SECRET, PASSPHRASE)
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert is_encrypted(path)
    assert read_secure(path, PASSPHRASE) == SECRET


def test_plaintext_write_is_readable_without_passphrase():
    path = _tmpdir() / "public.bin"
    write_secure(path, SECRET, None, allow_plaintext=True)
    assert not is_encrypted(path)
    assert read_secure(path, None) == SECRET


def test_encrypted_read_without_passphrase_raises():
    path = _tmpdir() / "locked.bin"
    write_secure(path, SECRET, PASSPHRASE)
    try:
        read_secure(path, None)
    except CryptoError as exc:
        assert "encrypted" in str(exc)
    else:
        raise AssertionError("must not read an encrypted file without a passphrase")


def test_guard_refuses_plaintext_when_encryption_enabled():
    """Regression: a seeding script once bypassed passphrase resolution and
    wrote an unencrypted corpus beside an encrypted index."""
    path = _tmpdir() / "oops.bin"
    os.environ["MEDRAG_ENCRYPT"] = "1"
    try:
        write_secure(path, SECRET, None)
    except CryptoError as exc:
        assert "refusing" in str(exc)
    else:
        raise AssertionError("must fail closed when MEDRAG_ENCRYPT is set")
    finally:
        os.environ.pop("MEDRAG_ENCRYPT", None)
    assert not path.exists(), "nothing should have been written"


def test_no_temp_file_left_behind():
    path = _tmpdir() / "clean.bin"
    write_secure(path, SECRET, PASSPHRASE)
    assert list(path.parent.glob("*.tmp")) == []


# ------------------------------------------------------------- integration


def test_corpus_roundtrip_encrypted_and_no_leak():
    path = _tmpdir() / "corpus.jsonl"
    save_corpus([DOC], path, passphrase=PASSPHRASE)

    raw = path.read_bytes()
    assert b"Distinctive Title" not in raw
    assert b"uniquely identifiable" not in raw

    docs = load_corpus(path, passphrase=PASSPHRASE)
    assert len(docs) == 1 and docs[0].title == DOC.title


def test_index_roundtrip_encrypted_and_no_leak():
    directory = _tmpdir()
    emb = HashingEmbedder(dim=256)
    chunks = chunk_document(DOC, Config())
    store = VectorStore(dim=emb.dim, embedder_name=emb.name)
    store.add(chunks, emb.embed([c.text for c in chunks]))
    store.save(directory, passphrase=PASSPHRASE)

    for name in ("chunks.jsonl", "index.faiss", "vectors.npy"):
        f = directory / name
        if f.exists():
            assert b"Distinctive Title" not in f.read_bytes(), f"{name} leaked plaintext"

    # the manifest is deliberately public, and must say a key is required
    import json

    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["encrypted"] is True

    reloaded = VectorStore.load(directory, passphrase=PASSPHRASE)
    assert len(reloaded) == len(chunks)
    hits = reloaded.search(emb.embed_query("uniquely identifiable phrase"), k=1)
    assert hits and hits[0].chunk.doc_id == "SAMPLE-001"


def test_index_load_with_wrong_passphrase_fails():
    directory = _tmpdir()
    emb = HashingEmbedder(dim=256)
    chunks = chunk_document(DOC, Config())
    store = VectorStore(dim=emb.dim, embedder_name=emb.name)
    store.add(chunks, emb.embed([c.text for c in chunks]))
    store.save(directory, passphrase=PASSPHRASE)

    try:
        VectorStore.load(directory, passphrase="wrong")
    except CryptoError:
        pass
    else:
        raise AssertionError("wrong passphrase must not load an index")


# ------------------------------------------------------------- offline mode


def test_offline_blocks_pubmed():
    for fn, args in ((pubmed.search_pubmed, ("q",)), (pubmed.fetch_pubmed, (["1"],))):
        try:
            fn(*args, cfg=Config(offline=True))
        except RuntimeError as exc:
            assert "offline" in str(exc)
        else:
            raise AssertionError(f"{fn.__name__} must refuse to run offline")


def test_offline_never_selects_openai_embedder():
    cfg = Config(offline=True, openai_api_key="sk-should-be-ignored")
    assert not get_embedder(cfg, verbose=False).name.startswith("openai:")


def test_offline_generator_has_no_client():
    from medrag.generator import Generator

    gen = Generator(Config(offline=True, openai_api_key="sk-should-be-ignored"))
    assert gen.client is None


def test_config_repr_never_exposes_secrets():
    cfg = Config(openai_api_key="sk-super-secret-value", passphrase="hunter2")
    text = repr(cfg)
    assert "sk-super-secret-value" not in text and "hunter2" not in text
    assert "set" in text


if __name__ == "__main__":
    np.random.seed(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("\nall crypto tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
