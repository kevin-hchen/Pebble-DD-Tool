"""Optional encryption at rest for the corpus and index.

Scope, stated plainly: this protects stored files against someone who obtains
the disk or the files but not the passphrase. It does NOT protect data in use
(decrypted content lives in process memory), does NOT protect anything sent to
OpenAI or NCBI, and does NOT anonymize the corpus. See SECURITY.md.

Construction:
  key      = scrypt(passphrase, salt, n=2**15, r=8, p=1) -> 32 bytes
  envelope = MAGIC | version | salt(16) | nonce(12) | AES-256-GCM ciphertext

AES-GCM is authenticated, so tampering with a stored index is detected on read
rather than silently deserialized. A fresh 12-byte nonce is generated per write:
GCM nonce reuse under the same key is catastrophic, so nonces are never derived
or reused, and the salt is per-file so two files never share a key.
"""

from __future__ import annotations

import os
import re
import secrets
import struct
from pathlib import Path

MAGIC = b"MEDRAG\x01"
VERSION = 1
SALT_LEN = 16
NONCE_LEN = 12
SCRYPT_N = 2**15  # ~32 MB, ~100 ms: deliberately slow against offline guessing
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32

ENV_PASSPHRASE = "MEDRAG_PASSPHRASE"


class CryptoError(RuntimeError):
    """Raised for wrong passphrase, tampered data, or malformed envelopes."""


def _require_backend():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        return AESGCM, Scrypt
    except ImportError as exc:  # pragma: no cover
        raise CryptoError(
            "encryption requires the 'cryptography' package: pip install cryptography"
        ) from exc


def derive_key(passphrase: str, salt: bytes) -> bytes:
    _, Scrypt = _require_backend()
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_bytes(plaintext: bytes, passphrase: str) -> bytes:
    """Return a self-describing encrypted envelope."""
    AESGCM, _ = _require_backend()
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = derive_key(passphrase, salt)
    # The header is authenticated as associated data, so the version and salt
    # cannot be swapped without failing the tag check.
    header = MAGIC + struct.pack("!B", VERSION) + salt + nonce
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, header)
    return header + ciphertext


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    AESGCM, _ = _require_backend()
    header_len = len(MAGIC) + 1 + SALT_LEN + NONCE_LEN
    if len(blob) < header_len or not blob.startswith(MAGIC):
        raise CryptoError("not a MedRAG encrypted file (bad magic or truncated)")

    version = struct.unpack("!B", blob[len(MAGIC) : len(MAGIC) + 1])[0]
    if version != VERSION:
        raise CryptoError(f"unsupported envelope version {version}")

    salt = blob[len(MAGIC) + 1 : len(MAGIC) + 1 + SALT_LEN]
    nonce = blob[len(MAGIC) + 1 + SALT_LEN : header_len]
    header, ciphertext = blob[:header_len], blob[header_len:]

    try:
        return AESGCM(derive_key(passphrase, salt)).decrypt(nonce, ciphertext, header)
    except Exception as exc:
        # GCM cannot distinguish a wrong key from tampering - both fail the tag.
        raise CryptoError(
            "decryption failed: wrong passphrase, or the file has been modified"
        ) from exc


def is_encrypted(path: str | Path) -> bool:
    path = Path(path)
    if not path.exists() or path.stat().st_size < len(MAGIC):
        return False
    with path.open("rb") as fh:
        return fh.read(len(MAGIC)) == MAGIC


def get_passphrase(required: bool = True, confirm: bool = False) -> str | None:
    """Resolve the passphrase from the environment, else prompt without echo."""
    pw = os.getenv(ENV_PASSPHRASE)
    if pw:
        return pw
    if not required:
        return None

    import getpass

    pw = getpass.getpass("MedRAG passphrase: ")
    if not pw:
        raise CryptoError("empty passphrase")
    if confirm and pw != getpass.getpass("Confirm passphrase: "):
        raise CryptoError("passphrases do not match")
    return pw


def write_secure(
    path: str | Path,
    data: bytes,
    passphrase: str | None,
    allow_plaintext: bool = False,
) -> Path:
    """Write bytes, encrypted when a passphrase is given, always mode 0600.

    Fail-closed guard: if encryption is enabled globally but this call was given
    no passphrase, refuse rather than silently writing cleartext. Caught a real
    bug where a seeding script bypassed the pipeline's passphrase resolution and
    wrote an unencrypted corpus next to an encrypted index. Callers that are
    genuinely meant to be public (the index manifest) opt out explicitly.
    """
    path = Path(path)
    if passphrase is None and not allow_plaintext:
        if str(os.getenv("MEDRAG_ENCRYPT", "")).strip().lower() in {"1", "true", "yes", "on"}:
            raise CryptoError(
                f"refusing to write {path.name} in plaintext while MEDRAG_ENCRYPT is set — "
                "the caller did not supply a passphrase"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encrypt_bytes(data, passphrase) if passphrase else data

    # Write via a private temp file then rename, so a crash cannot leave a
    # half-written index and the file is never briefly world-readable.
    #
    # The temp name is unique per call. It used to be a fixed `<name>.tmp`, which
    # two concurrent writers shared: they interleaved into one temp file, and the
    # `finally` below deleted the other's temp out from under it, so one ingest
    # silently lost its records and the other died on the rename. A run of the
    # Streamlit app alongside a CLI ingest is enough to hit that.
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # The rename itself needs the directory flushed, or a power loss can lose
        # it and leave the old file behind while reporting success.
        _fsync_dir(path.parent)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    os.chmod(path, 0o600)
    return path


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory flush; not available on every platform."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows has no directory handle to fsync
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def read_secure(path: str | Path, passphrase: str | None) -> bytes:
    """Read bytes, decrypting if the file carries the MedRAG envelope."""
    path = Path(path)
    blob = path.read_bytes()
    if not blob.startswith(MAGIC):
        return blob  # stored in the clear
    if not passphrase:
        raise CryptoError(
            f"{path.name} is encrypted - set {ENV_PASSPHRASE} or pass --encrypt to be prompted"
        )
    return decrypt_bytes(blob, passphrase)


def harden_permissions(path: str | Path) -> None:
    """Best-effort chmod 0600 (files) / 0700 (directories); silent on Windows."""
    path = Path(path)
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except (OSError, NotImplementedError):  # pragma: no cover
        pass


def unguessable_stem(base: str, fallback: str = "export") -> str:
    """A filename stem that keeps its human label but cannot be guessed.

    Every exporter derived its filename purely from user input — the asset, or
    on the claims page the COMPANY NAME. Two consequences on any shared or
    served machine: two people looking at the same asset write to the same file
    and can read each other's output, and the path of a memo about a named
    company is guessable by anyone who knows the company.

    The label is kept because an operator has to find their own file in `out/`,
    and a directory of pure hex is its own kind of unusable. The random suffix
    is what makes it unguessable; 8 bytes of `token_hex` is 64 bits, far beyond
    what a directory of a few hundred memos needs to avoid both collision and
    enumeration.

    `secrets`, not `random`: the property wanted is unpredictability, and
    reaching for the non-cryptographic generator is how a "random" filename ends
    up derivable from a seed.
    """
    label = re.sub(r"[^a-z0-9]+", "-", (base or "").lower()).strip("-") or fallback
    return f"{label[:60]}-{secrets.token_hex(8)}"


def harden_outputs(directory: str | Path, *files: str | Path) -> None:
    """Lock down a generated memo and the directory holding it.

    `out/` was the hole in an otherwise careful story. The corpus is 0600 and
    optionally AES-encrypted, but every exporter wrote its Markdown and PDF with
    a plain `write_text`, landing at 0644 — world-readable on a shared machine.
    The memo is the *more* sensitive artifact of the two: a claims memo carries
    the deck-derived claims, the company name and the asset under diligence,
    where the corpus carries published abstracts.

    This is the floor, not the ceiling. These files are still plaintext even when
    MEDRAG_ENCRYPT is set, because they are meant to be opened by a PDF reader and
    circulated. Bringing them inside the encryption boundary is a separate design
    question, not a chmod.
    """
    harden_permissions(directory)
    for f in files:
        harden_permissions(f)
