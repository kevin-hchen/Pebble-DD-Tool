"""openFDA FAERS aggregate fixtures — real `count` responses (2026-08-06).

Captured live for pembrolizumab. The shapes here are what the guard must
survive, and each was measured rather than assumed:

  * REACTIONS_PAGE's top term is MALIGNANT NEOPLASM PROGRESSION (12,012) — the
    cancer progressing, i.e. the indication, not a drug effect. Any renderer
    that prints this table without saying so invites the reader to read disease
    progression as toxicity.
  * ROLE_PAGE contains a bucket for code "4", which the FDA data dictionary does
    not define (it documents only 1 Suspect, 2 Concomitant, 3 Interacting). It
    must render as undefined, never as a bare number a reader takes for a
    category.
  * ROLE_PAGE shows 38,654 CONCOMITANT reports against 104,464 suspect: a third
    of the reports counted for this drug record it as merely present alongside
    the drug actually suspected.
  * REPORTER_PAGE includes lawyer-filed reports, which are a litigation artefact
    before they are a safety signal.

openFDA returns aggregate buckets as {"term": ..., "count": ...}; a `count`
query returns no `meta.results.total`, which is why report totals are fetched
separately.
"""

REACTIONS_PAGE = {'meta': {'disclaimer': 'Do not rely on openFDA to make decisions regarding medical care. '
                        'While we make every effort to ensure that data is accurate, you '
                        'should assume all results are unvalidated. We may limit or '
                        'otherwise restrict your access to the API in line with our Terms of '
                        'Service.',
          'terms': 'https://open.fda.gov/terms/',
          'license': 'https://open.fda.gov/license/',
          'last_updated': '2026-07-30'},
 'results': [{'term': 'MALIGNANT NEOPLASM PROGRESSION', 'count': 12012},
             {'term': 'DEATH', 'count': 5848},
             {'term': 'DIARRHOEA', 'count': 5719},
             {'term': 'FATIGUE', 'count': 5113},
             {'term': 'OFF LABEL USE', 'count': 4755},
             {'term': 'PYREXIA', 'count': 3870},
             {'term': 'RASH', 'count': 3719},
             {'term': 'NAUSEA', 'count': 3563},
             {'term': 'PRODUCT USE IN UNAPPROVED INDICATION', 'count': 3541},
             {'term': 'DECREASED APPETITE', 'count': 3305}]}

SERIOUS_PAGE = {'meta': {'disclaimer': 'Do not rely on openFDA to make decisions regarding medical care. '
                        'While we make every effort to ensure that data is accurate, you '
                        'should assume all results are unvalidated. We may limit or '
                        'otherwise restrict your access to the API in line with our Terms of '
                        'Service.',
          'terms': 'https://open.fda.gov/terms/',
          'license': 'https://open.fda.gov/license/',
          'last_updated': '2026-07-30'},
 'results': [{'term': 1, 'count': 91519}, {'term': 2, 'count': 13090}]}

REPORTER_PAGE = {'meta': {'disclaimer': 'Do not rely on openFDA to make decisions regarding medical care. '
                        'While we make every effort to ensure that data is accurate, you '
                        'should assume all results are unvalidated. We may limit or '
                        'otherwise restrict your access to the API in line with our Terms of '
                        'Service.',
          'terms': 'https://open.fda.gov/terms/',
          'license': 'https://open.fda.gov/license/',
          'last_updated': '2026-07-30'},
 'results': [{'term': 1, 'count': 51356},
             {'term': 5, 'count': 23792},
             {'term': 3, 'count': 21376},
             {'term': 2, 'count': 7463},
             {'term': 4, 'count': 41}]}

ROLE_PAGE = {'meta': {'disclaimer': 'Do not rely on openFDA to make decisions regarding medical care. '
                        'While we make every effort to ensure that data is accurate, you '
                        'should assume all results are unvalidated. We may limit or '
                        'otherwise restrict your access to the API in line with our Terms of '
                        'Service.',
          'terms': 'https://open.fda.gov/terms/',
          'license': 'https://open.fda.gov/license/',
          'last_updated': '2026-07-30'},
 'results': [{'term': 1, 'count': 104464},
             {'term': 2, 'count': 38654},
             {'term': 3, 'count': 347},
             {'term': 4, 'count': 4}]}

#: Report totals, fetched separately because a count query returns no total.
TOTALS = {"faers_total": 20692690, "normalised": 104614, "free_text": 105073}
