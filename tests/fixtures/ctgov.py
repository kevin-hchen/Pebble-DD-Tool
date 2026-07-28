"""ClinicalTrials.gov API v2 response fixtures.

Structured to match the real v2 payload shape: everything under protocolSection,
modules that may be absent, phases as a list, enrollment split into count and
type, and pagination by opaque nextPageToken.

The set deliberately includes the awkward records, because those are what break
parsers in production: a TERMINATED trial with whyStopped filled, a WITHDRAWN
trial with whyStopped absent (the common case - the sponsor filed nothing), a
two-phase trial, a record missing most optional modules, and a record with no
NCT ID at all that must be skipped rather than crash the batch.
"""

PAGE_ONE = {
    "totalCount": 5,
    "nextPageToken": "TOKEN_PAGE_2",
    "studies": [
        {
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT03057977",
                    "briefTitle": "Empagliflozin Outcome Trial in Heart Failure With Preserved Ejection Fraction",
                },
                "statusModule": {
                    "overallStatus": "COMPLETED",
                    "startDateStruct": {"date": "2017-03-27", "type": "ACTUAL"},
                    "primaryCompletionDateStruct": {"date": "2021-04-27", "type": "ACTUAL"},
                    "completionDateStruct": {"date": "2021-05-26", "type": "ACTUAL"},
                },
                "designModule": {
                    "studyType": "INTERVENTIONAL",
                    "phases": ["PHASE3"],
                    "enrollmentInfo": {"count": 5988, "type": "ACTUAL"},
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Boehringer Ingelheim", "class": "INDUSTRY"},
                    "collaborators": [{"name": "Eli Lilly and Company", "class": "INDUSTRY"}],
                },
                "conditionsModule": {"conditions": ["Heart Failure"]},
                "armsInterventionsModule": {
                    "interventions": [
                        {"type": "DRUG", "name": "Empagliflozin"},
                        {"type": "DRUG", "name": "Placebo"},
                    ]
                },
            }
        },
        {
            # The record that matters most for diligence: stopped, with a reason.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT01234567",
                    "briefTitle": "Study of Compound X in Advanced Solid Tumors",
                },
                "statusModule": {
                    "overallStatus": "TERMINATED",
                    "whyStopped": "Interim analysis did not meet prespecified efficacy boundary",
                    "startDateStruct": {"date": "2019-06-01"},
                    "primaryCompletionDateStruct": {"date": "2021-02-15"},
                },
                "designModule": {
                    "studyType": "INTERVENTIONAL",
                    "phases": ["PHASE2"],
                    # Actual enrollment far below plan is itself a signal.
                    "enrollmentInfo": {"count": 47, "type": "ACTUAL"},
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Example Therapeutics", "class": "INDUSTRY"}
                },
                "conditionsModule": {"conditions": ["Solid Tumor", "Neoplasms"]},
                "armsInterventionsModule": {
                    "interventions": [{"type": "DRUG", "name": "Compound X"}]
                },
            }
        },
    ],
}

PAGE_TWO = {
    "totalCount": 5,
    # No nextPageToken: this is the last page.
    "studies": [
        {
            # Stopped, no reason given. The common and more dangerous case.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT07654321",
                    "briefTitle": "Withdrawn Study of Compound X in Renal Impairment",
                },
                "statusModule": {
                    "overallStatus": "WITHDRAWN",
                    "startDateStruct": {"date": "2020-01-15"},
                },
                "designModule": {
                    "studyType": "INTERVENTIONAL",
                    "phases": ["PHASE1"],
                    "enrollmentInfo": {"count": 0, "type": "ACTUAL"},
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Example Therapeutics", "class": "INDUSTRY"}
                },
                "conditionsModule": {"conditions": ["Renal Insufficiency"]},
                "armsInterventionsModule": {
                    "interventions": [{"type": "DRUG", "name": "Compound X"}]
                },
            }
        },
        {
            # Two phases at once, and an estimated (not actual) enrollment.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT05555555",
                    "briefTitle": "Seamless Phase 2/3 Study of Compound Y",
                },
                "statusModule": {
                    "overallStatus": "RECRUITING",
                    "startDateStruct": {"date": "2024-09-01"},
                    "primaryCompletionDateStruct": {"date": "2027-03-01"},
                },
                "designModule": {
                    "studyType": "INTERVENTIONAL",
                    "phases": ["PHASE2", "PHASE3"],
                    "enrollmentInfo": {"count": 1200, "type": "ESTIMATED"},
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Rival Biosciences", "class": "INDUSTRY"}
                },
                "conditionsModule": {"conditions": ["Heart Failure"]},
                "armsInterventionsModule": {
                    "interventions": [{"type": "DRUG", "name": "Compound Y"}]
                },
            }
        },
        {
            # Nearly empty record: only identification. Must not crash.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT09999999",
                    "briefTitle": "Sparse Registry Record",
                }
            }
        },
        {
            # No NCT ID: unusable, must be skipped without killing the batch.
            "protocolSection": {
                "identificationModule": {"briefTitle": "Malformed record with no ID"}
            }
        },
    ],
}

EMPTY_PAGE = {"totalCount": 0, "studies": []}
