"""openFDA device API response fixtures.

Built from ACTUAL api.fda.gov payloads for product code FRN (infusion pumps),
captured live, then trimmed only in the longest free-text fields (marked […]) and
in the recalls' k_numbers arrays (real values, capped to 3 — a single recall can
reference hundreds of cleared predicates). The awkward realities the parsers must
survive are all here: device/event nests its product code under
device[].device_report_product_code (there is no top-level product_code),
event.date_received is YYYYMMDD while 510k/recall dates are ISO,
statement_or_summary is often empty, and recall_status varies ("Terminated" vs
"Open, Classified"). The RECALLS page includes the real Baxter Colleague
infusion-pump recall; the EVENTS page spans Death / Injury / Malfunction so
severity ordering can be exercised.
"""

CLEARANCE_PAGE = {'meta': {'results': {'skip': 0, 'limit': 2, 'total': 848}},
 'results': [{'k_number': 'K781171',
              'applicant': 'Imed Corp.',
              'device_name': 'INFUSION PUMP, PEDIATRIC MODEL 301',
              'product_code': 'FRN',
              'decision_code': 'SESE',
              'decision_description': 'Substantially Equivalent',
              'decision_date': '1978-08-10',
              'date_received': '1978-07-13',
              'clearance_type': 'Traditional',
              'advisory_committee': 'HO',
              'statement_or_summary': '',
              'openfda': {'device_class': '2',
                          'regulation_number': '880.5725',
                          'medical_specialty_description': 'General Hospital'}},
             {'k_number': 'K931318',
              'applicant': 'Graseby Medical , Ltd.',
              'device_name': '3400 INFUSION PUMP',
              'product_code': 'FRN',
              'decision_code': 'ST',
              'decision_description': 'Substantially Equivalent - Subject to Tracking Reg.',
              'decision_date': '1994-12-09',
              'date_received': '1993-03-16',
              'clearance_type': 'Traditional',
              'advisory_committee': 'HO',
              'statement_or_summary': '',
              'openfda': {'device_class': '2',
                          'regulation_number': '880.5725',
                          'medical_specialty_description': 'General Hospital'}}]}

RECALL_PAGE = {'meta': {'results': {'skip': 0, 'limit': 3, 'total': 718}},
 'results': [{'product_res_number': 'Z-0001-2011',
              'cfres_id': '93646',
              'res_event_number': '56425',
              'product_code': 'FRN',
              'product_description': 'Baxter Colleague Single Channel Volumetric Infusion Pumps. '
                                     'Baxter Healthcare Corporation, Medication Delivery Division. '
                                     'Product Codes: 2M8151, 2M8161, and 2M9161.',
              'reason_for_recall': 'The FDA sent a letter to Baxter on April 30, 2010, ordering '
                                   'the company to recall and destroy all models of its Colleague '
                                   'Volumetric Infusion Pumps currently in use in the United '
                                   'States.  FDA determined that this action […]',
              'recalling_firm': 'Baxter Healthcare Corp.',
              'recall_status': 'Terminated',
              'root_cause_description': 'Device Design',
              'event_date_initiated': '2010-08-04',
              'event_date_posted': '2010-10-12',
              'k_numbers': ['K063696'],
              'openfda': {'device_class': '2'}},
             {'product_res_number': 'Z-0002-2011',
              'cfres_id': '93647',
              'res_event_number': '56425',
              'product_code': 'FRN',
              'product_description': 'Baxter Colleague Triple Channel Volumetric Infusion Pumps. '
                                     'Baxter Healthcare Corporation, Medication Delivery Division. '
                                     'Product codes: 2M8153, and 2M8163.',
              'reason_for_recall': 'The FDA sent a letter to Baxter on April 30, 2010, ordering '
                                   'the company to recall and destroy all models of its Colleague '
                                   'Volumetric Infusion Pumps currently in use in the United '
                                   'States.  FDA determined that this action […]',
              'recalling_firm': 'Baxter Healthcare Corp.',
              'recall_status': 'Terminated',
              'root_cause_description': 'Device Design',
              'event_date_initiated': '2010-08-04',
              'event_date_posted': '2010-10-12',
              'k_numbers': ['K063696'],
              'openfda': {'device_class': '2'}},
             {'product_res_number': 'Z-0005-2025',
              'cfres_id': '210103',
              'res_event_number': '95382',
              'product_code': 'FRN',
              'product_description': 'Z-800 Infusion System, Model Numbers Z-800, Z-800F, Z-800W, '
                                     'Z-800WF;\n'
                                     'Software Version: Z-800 6.1.01 and 6-1.07z; Z-800F 4.1.02 '
                                     'and 4.1.08z; Z-800W 3.1.32 and 3.1.64z, Z-800WF 3.1.32 and '
                                     '3.1.64z',
              'reason_for_recall': 'There is a defect in the air-in-line software algorithm.',
              'recalling_firm': 'Zyno Medical LLC',
              'recall_status': 'Open, Classified',
              'root_cause_description': 'Process change control',
              'event_date_initiated': '2024-09-13',
              'event_date_posted': '2024-10-09',
              'k_numbers': ['K100705', 'K130690'],
              'openfda': {'device_class': '2'}}]}

EVENT_PAGE = {'meta': {'results': {'skip': 0, 'limit': 3, 'total': 1822721}},
 'results': [{'report_number': '6000001-2008-00061',
              'mdr_report_key': '1001903',
              'event_type': 'Death',
              'date_received': '20080221',
              'product_problems': ['Use of Device Problem'],
              'device': [{'brand_name': 'COLLEAGUE TRIPLE CHANNEL VOLUMETRIC PUMP CE ENGLISH',
                          'generic_name': '80FRN',
                          'device_report_product_code': 'FRN',
                          'manufacturer_d_name': 'BAXTER HEALTHCARE PTE LTD',
                          'openfda': {'device_class': '2'}}],
              'mdr_text': [{'text_type_code': 'Description of Event or Problem',
                            'text': 'A PRODUCT COMPLAINT NOTIFICATION FORM AND BAXTER MEDICAL '
                                    'DEVICE FORM WERE RECEIVED FROM BAXTER ANOTHER COUNTRY ON '
                                    '02/17/2008 INDICATING THAT A COLLEAGUE TRIPLE CHANNEL PUMP '
                                    'WAS ADMINISTERING PENICILLI […]'}]},
             {'report_number': '2032227-2020-110177',
              'mdr_report_key': '10000010',
              'event_type': 'Injury',
              'date_received': '20200427',
              'product_problems': ['Excess Flow or Over-Infusion', 'Excess Flow or Over-Infusion'],
              'device': [{'brand_name': 'RESERVOIR 3ML MMT-332A',
                          'generic_name': 'PUMP, INFUSION',
                          'device_report_product_code': 'FRN',
                          'manufacturer_d_name': 'MEDTRONIC PUERTO RICO OPERATIONS CO.',
                          'openfda': {'device_class': '2'}}],
              'mdr_text': [{'text_type_code': 'Additional Manufacturer Narrative',
                            'text': '(B)(4). CURRENTLY IT IS UNKNOWN WHETHER OR NOT THE DEVICE MAY '
                                    'HAVE CAUSED OR CONTRIBUTED TO THE EVENT AS NO PRODUCT HAS '
                                    'BEEN RETURNED. THE DEVICE WILL BE RETURNED FOR ANALYSIS AND '
                                    'FURTHER INFORMATION […]'}]},
             {'report_number': '2032227-2020-110243',
              'mdr_report_key': '10000149',
              'event_type': 'Malfunction',
              'date_received': '20200427',
              'product_problems': ['Fluid/Blood Leak', 'Fluid/Blood Leak'],
              'device': [{'brand_name': 'RESERVOIR 2PK 3ML MMT-332AT',
                          'generic_name': 'PUMP, INFUSION',
                          'device_report_product_code': 'FRN',
                          'manufacturer_d_name': 'MEDTRONIC PUERTO RICO OPERATIONS CO.',
                          'openfda': {'device_class': '2'}}],
              'mdr_text': [{'text_type_code': 'Additional Manufacturer Narrative',
                            'text': '(B)(4). CURRENTLY IT IS UNKNOWN WHETHER OR NOT THE DEVICE MAY '
                                    'HAVE CAUSED OR CONTRIBUTED TO THE EVENT AS NO PRODUCT HAS '
                                    'BEEN RETURNED. NO CONCLUSION CAN BE DRAWN AT THIS TIME. WE '
                                    'THEREFORE CONSIDER TH […]'}]}]}

# openFDA answers "no matches" with HTTP 404 and an error body, not an empty list.
NOT_FOUND = {"error": {"code": "NOT_FOUND", "message": "No matches found!"}}
