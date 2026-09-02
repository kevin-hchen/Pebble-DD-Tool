"""The consent record: a timestamp and a terms version, and structurally
nothing else.

The terms say we record two values. That is a promise about a data structure, so
it is enforced at the type level rather than by remembering not to add fields —
the same construction as `FAERSAnswer`, which has no field or method whose name
means "rate" because FAERS has no denominator and a rate would be a lie the type
system can prevent.

`__slots__` is the mechanism. It is not a memory optimisation here: it means an
attribute this class does not declare CANNOT be set on an instance, so
`record.query = "..."` raises AttributeError at the moment someone tries it
rather than quietly attaching a copy of a visitor's search to an object that
gets held somewhere. `tests/test_public_app.py` asserts by inspection that no
declared field can hold content, so adding one fails the suite.

WHY CONSENT IS PER SUBMISSION
-----------------------------
Not a cookie, not a session flag, not "you agreed last time". Each submission
carries its own consent, unchecked by default, because the thing being consented
to is the transmission of THAT material. A remembered tick would mean a visitor
who agreed once, to send one document, has agreed to send every future one —
which is exactly the reasoning in `claims.py`, where a provider setting chosen
weeks ago is not consent for today's deck.

There is deliberately no store, no session table and no cookie in this module. A
consent record is created, counted, and dropped when the response is sent. If a
deployment ever needs to prove consent was given, that is a new requirement with
a new terms section, not a field quietly added here.
"""

from __future__ import annotations

from datetime import datetime, timezone


class ConsentRequired(RuntimeError):
    """A submission that needed consent did not carry it.

    Raised before any material is read, parsed or transmitted — the same
    ordering as `ClaimVerifier.verify`, which raises before touching a store or
    a model, so an unconsented submission cannot have partially happened.
    """


class ConsentRecord:
    """Timestamp and terms version. Nothing else can be set on it.

    Both fields are read-only after construction, so a record cannot be
    retro-fitted with content by a later assignment either.
    """

    # The complete list of what may exist on an instance. Adding to it is a
    # terms change; `__slots__` makes anything absent from it unsettable.
    __slots__ = ("_at", "_terms_version")

    def __init__(self, terms_version: str, at: datetime | None = None) -> None:
        if not terms_version:
            raise ValueError(
                "a consent record needs the terms version it was given under; "
                "consent to an unnamed version cannot be honoured later")
        self._at = at or datetime.now(timezone.utc)
        self._terms_version = terms_version

    @property
    def at(self) -> datetime:
        return self._at

    @property
    def terms_version(self) -> str:
        return self._terms_version

    def __repr__(self) -> str:
        return (f"ConsentRecord(at={self._at.isoformat()}, "
                f"terms_version={self._terms_version!r})")


def require_consent(given: bool, terms_version: str) -> ConsentRecord:
    """Turn a form checkbox into a record, or refuse.

    `given` comes from an HTML checkbox, which is absent from the form body when
    unticked — so the default is False by construction rather than by a default
    argument someone could change. There is no path here that consults a cookie,
    a session or a previous request.
    """
    if not given:
        raise ConsentRequired(
            "This submission was not sent. Tick the consent box to confirm you have "
            "read how the text you submit is handled. Consent is asked for every "
            "submission and is never carried over from a previous one.")
    return ConsentRecord(terms_version=terms_version)
