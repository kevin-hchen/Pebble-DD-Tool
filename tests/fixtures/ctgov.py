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


# --------------------------------------------------------------------------
# Trial-landscape fixtures: colorectal-cancer trials with the eligibility,
# location and contact modules populated, spanning every biomarker outcome an
# MSS patient can hit. Kept separate from PAGE_ONE/PAGE_TWO so the diligence
# suites' exact counts are undisturbed.
#
# By design it includes: a direct-MSS recruiting trial with sites and a contact;
# a trial that expresses MSS only INDIRECTLY by excluding MSI-H (must resolve to
# UNCLEAR, not a silent drop); a trial that requires MSI-H (EXCLUDED for an MSS
# patient); a trial that never mentions the biomarker; an eligible-but-closed
# trial (sorts below the open ones); and a trial with no eligibility text at all.

LANDSCAPE_PAGE = {
    "totalCount": 6,
    "studies": [
        {   # A — direct MSS, recruiting, sites in Boston and Houston.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT10000001",
                    "briefTitle": "Immunotherapy Combination in MSS Metastatic Colorectal Cancer",
                },
                "statusModule": {"overallStatus": "RECRUITING",
                                 "startDateStruct": {"date": "2024-02-01"}},
                "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE2"],
                                 "enrollmentInfo": {"count": 120, "type": "ESTIMATED"}},
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Dana-Farber Cancer Institute", "class": "OTHER"}},
                "conditionsModule": {"conditions": ["Colorectal Cancer", "Metastatic Colorectal Cancer"]},
                "armsInterventionsModule": {"interventions": [
                    {"type": "DRUG", "name": "Botensilimab"}, {"type": "DRUG", "name": "Balstilimab"}]},
                "eligibilityModule": {
                    "eligibilityCriteria": (
                        "Inclusion Criteria:\n\n"
                        "* Histologically confirmed metastatic colorectal adenocarcinoma\n"
                        "* Microsatellite stable (MSS) or proficient mismatch repair (pMMR) by IHC or PCR\n"
                        "* ECOG performance status 0-1\n\n"
                        "Exclusion Criteria:\n\n"
                        "* Prior immune checkpoint inhibitor therapy\n"
                    ),
                    "minimumAge": "18 Years", "maximumAge": "", "sex": "ALL",
                    "healthyVolunteers": False,
                },
                "contactsLocationsModule": {
                    "overallOfficials": [
                        {"name": "Jane A. Smith, MD", "role": "PRINCIPAL_INVESTIGATOR",
                         "affiliation": "Dana-Farber Cancer Institute"}],
                    "centralContacts": [
                        {"name": "CRC Trial Coordinator", "email": "crc-trials@dfci.example",
                         "phone": "617-555-0100"}],
                    "locations": [
                        {"facility": "Dana-Farber Cancer Institute", "city": "Boston",
                         "state": "Massachusetts", "country": "United States", "status": "RECRUITING",
                         # Per-site contacts, exactly as the live API nests them on
                         # recruiting trials: a reachable coordinator and a site PI.
                         "contacts": [
                             {"name": "Site Public Contact", "role": "CONTACT",
                              "phone": "617-555-0142", "email": "boston-crc@dfci.example"},
                             {"name": "Alan Boston, MD", "role": "PRINCIPAL_INVESTIGATOR"}]},
                        {"facility": "MD Anderson", "city": "Houston", "state": "Texas",
                         "country": "United States", "status": "RECRUITING",
                         "contacts": [
                             {"name": "Houston Study Coordinator", "role": "CONTACT",
                              "phone": "713-555-0177"}]}],
                },
                "descriptionModule": {"briefSummary": "A phase 2 study of dual checkpoint blockade in MSS mCRC."},
            }
        },
        {   # B — INDIRECT MSS: expresses it only by excluding MSI-H -> UNCLEAR.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT10000002",
                    "briefTitle": "Targeted Therapy in Advanced Colorectal Cancer",
                },
                "statusModule": {"overallStatus": "RECRUITING",
                                 "startDateStruct": {"date": "2023-11-15"}},
                "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE1", "PHASE2"],
                                 "enrollmentInfo": {"count": 80, "type": "ESTIMATED"}},
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "Memorial Sloan Kettering Cancer Center", "class": "OTHER"}},
                "conditionsModule": {"conditions": ["Colorectal Cancer"]},
                "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Compound Z"}]},
                "eligibilityModule": {
                    "eligibilityCriteria": (
                        "Inclusion Criteria:\n\n"
                        "* Advanced or metastatic colorectal cancer\n"
                        "* Measurable disease per RECIST 1.1\n\n"
                        "Exclusion Criteria:\n\n"
                        "* Known MSI-H or dMMR tumors\n"
                        "* Active autoimmune disease\n"
                    ),
                    "minimumAge": "18 Years", "sex": "ALL", "healthyVolunteers": False,
                },
                "contactsLocationsModule": {
                    "overallOfficials": [
                        {"name": "Robert Lee, MD", "role": "STUDY_DIRECTOR",
                         "affiliation": "Memorial Sloan Kettering"}],
                    "centralContacts": [{"name": "Clinical Trials Office", "phone": "212-555-0199"}],
                    "locations": [
                        {"facility": "MSKCC", "city": "New York", "state": "New York",
                         "country": "United States", "status": "RECRUITING"}],
                },
                "descriptionModule": {"briefSummary": "A study restricting enrolment to non-MSI-H tumors."},
            }
        },
        {   # C — requires MSI-H: an MSS patient is EXCLUDED (not shown).
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT10000003",
                    "briefTitle": "Pembrolizumab in MSI-High Colorectal Cancer",
                },
                "statusModule": {"overallStatus": "RECRUITING"},
                "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE3"]},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Merck", "class": "INDUSTRY"}},
                "conditionsModule": {"conditions": ["Colorectal Cancer"]},
                "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Pembrolizumab"}]},
                "eligibilityModule": {
                    "eligibilityCriteria": (
                        "Inclusion Criteria:\n\n"
                        "* Metastatic colorectal cancer\n"
                        "* Tumors must be MSI-H (microsatellite instability-high) or dMMR\n"
                    ),
                    "sex": "ALL",
                },
                "contactsLocationsModule": {
                    "locations": [{"facility": "Site 1", "city": "Chicago", "state": "Illinois",
                                   "country": "United States", "status": "RECRUITING"}]},
            }
        },
        {   # D — biomarker never mentioned: NOT MENTIONED (not shown).
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT10000004",
                    "briefTitle": "Chemotherapy Optimization in Colorectal Cancer",
                },
                "statusModule": {"overallStatus": "RECRUITING"},
                "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE2"]},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "SWOG", "class": "NETWORK"}},
                "conditionsModule": {"conditions": ["Colorectal Cancer"]},
                "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "FOLFOX"}]},
                "eligibilityModule": {
                    "eligibilityCriteria": (
                        "Inclusion Criteria:\n\n"
                        "* Stage III colorectal cancer\n"
                        "* Age 18 years or older\n"
                        "* ECOG 0-2\n"
                    ),
                    "sex": "ALL",
                },
                "contactsLocationsModule": {
                    "locations": [{"facility": "Site 2", "city": "Seattle", "state": "Washington",
                                   "country": "United States", "status": "RECRUITING"}]},
            }
        },
        {   # E — MSS but CLOSED to enrolment: ELIGIBLE, sorts below the open ones.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT10000005",
                    "briefTitle": "Completed Study of pMMR Colorectal Cancer, Boston",
                },
                "statusModule": {"overallStatus": "ACTIVE_NOT_RECRUITING"},
                "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE2"]},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Beth Israel", "class": "OTHER"}},
                "conditionsModule": {"conditions": ["Colorectal Cancer"]},
                "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Regorafenib"}]},
                "eligibilityModule": {
                    "eligibilityCriteria": (
                        "Inclusion Criteria:\n\n"
                        "* Colorectal cancer with proficient mismatch repair (pMMR)\n"
                    ),
                    "sex": "ALL",
                },
                "contactsLocationsModule": {
                    "locations": [{"facility": "BIDMC", "city": "Boston", "state": "Massachusetts",
                                   "country": "United States", "status": "ACTIVE_NOT_RECRUITING"}]},
            }
        },
        {   # F — no eligibility text at all: NOT MENTIONED + warning it wasn't screened.
            "protocolSection": {
                "identificationModule": {
                    "nctId": "NCT10000006",
                    "briefTitle": "Registry Record With No Eligibility Text",
                },
                "statusModule": {"overallStatus": "RECRUITING"},
                "conditionsModule": {"conditions": ["Colorectal Cancer"]},
                "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Unknown"}]},
            }
        },
    ],
}
