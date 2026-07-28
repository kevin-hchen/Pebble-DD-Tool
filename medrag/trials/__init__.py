"""ClinicalTrials.gov ingestion and the structured trial store.

Deliberately separate from the literature path. Trial records are facts with
fields - phase, status, sponsor, enrollment - and those are filters, not
semantics. Embedding them as prose and hoping cosine similarity recovers
"Phase 3 and TERMINATED" destroys the precision the registry is for.
"""

from .client import TrialRecord, fetch_trials, search_trials  # noqa: F401
from .store import TrialStore  # noqa: F401
