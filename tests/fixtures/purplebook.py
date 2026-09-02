"""Purple Book fixtures — real rows from the FDA monthly CSV (June 2026).

PURPLE_BOOK_CSV reproduces the published file's TWO-SECTION shape: a changes
report first, then the full database under an identical header. Taking the first
section would silently reduce the Purple Book to one month of changes, which is
why load_delimited takes a `section` argument and this fixture carries a decoy
row in section 1.

Rows chosen by measurement to cover what matters:
  * an adalimumab originator (351(a)) — the reference product with the most
    licensed biosimilars in the real data (54 product rows across 10 BLAs);
  * an adalimumab 351(k) INTERCHANGEABLE and a plain 351(k) BIOSIMILAR, so the
    two findings can be told apart;
  * pembrolizumab, a licensed biologic with NO licensed biosimilar — absence
    variant 3, the one specific to this source;
  * rows carrying orphan and reference-product exclusivity dates, which are
    populated on 25.6% and 1.6% of the real file respectively;
  * a legacy 5-digit BLA number, which broke the first version of the recon
    aligner (6-digit was assumed).
"""

PURPLE_BOOK_CSV = 'Purple Book Monthly Historical Data Changes Report - June 2026\r\n\r\nNewly Approved Products (N) ...\r\nN/R/U,Applicant,BLA Number,Proprietary Name,Proper Name,License Type,Strength,Dosage Form,Route of Administration,Product Presentation,Marketing Status,Licensure,Approval Date,Inter. Approval Date,Ref. Product Proper Name,Ref. Product Proprietary Name,Supplement Number,Submission Type,Inter. Supplement Number,License Number,Product Number,Center,Date of First Licensure,Exclusivity Expiration Date,First Interchangeable Exclusivity Exp. Date,Ref. Product Exclusivity Exp. Date,Orphan Exclusivity Exp. Date,Patent List Provided\r\nN,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x,x\r\n\r\nN/R/U,Applicant,BLA Number,Proprietary Name,Proper Name,License Type,Strength,Dosage Form,Route of Administration,Product Presentation,Marketing Status,Licensure,Approval Date,Inter. Approval Date,Ref. Product Proper Name,Ref. Product Proprietary Name,Supplement Number,Submission Type,Inter. Supplement Number,License Number,Product Number,Center,Date of First Licensure,Exclusivity Expiration Date,First Interchangeable Exclusivity Exp. Date,Ref. Product Exclusivity Exp. Date,Orphan Exclusivity Exp. Date,Patent List Provided\r\n,AbbVie Inc.,125057,Humira,adalimumab,351(a),40MG/0.8ML,Injection,Subcutaneous,Autoinjector,Rx,Licensed,31-Dec-02,,N/A,N/A,,Original,,1889,001,CDER,,,,,24-Feb-28,YES\r\n,Amgen Inc.,761024,Amjevita,adalimumab-atto,351(k) Interchangeable,20MG/0.4ML,Injection,Subcutaneous,Pre-Filled Syringe,Rx,Licensed,23-Sep-16,20-Aug-24,adalimumab,Humira,,Original,19,1080,001,CDER,,,,,,\r\n,Hong Kong King-Friend Industrial Company Limited,761216,Yusimry,adalimumab-aqvh,351(k) Biosimilar,40MG/0.8ML,Injection,Subcutaneous,Pre-Filled Syringe,Rx,Licensed,17-Dec-21,,adalimumab,Humira,,Original,,2375,001,CDER,,,,,,\r\n,MSD International Business GmbH,125514,Keytruda,pembrolizumab,351(a),50MG,For Injection,Intravenous,Single-Dose Vial,Disc,Licensed,4-Sep-14,,N/A,N/A,,Original,,2405,001,CDER,,,,,25-Jan-31,\r\n,"Recordati Rare Diseases, Inc.",101246,Panhematin,Hemin for Injection,351(a),350MG,For Injection,Intravenous,Single-Dose Vial,Rx,Licensed,20-Jul-83,, ,,5350,Supplement,,1899,001,CBER,,,,,20-Jul-90,\r\n,Protein Sciences Corporation,125285,Flublok,Influenza Vaccine,351(a),135UG/.5ML,Injection,Intramuscular,Single-Dose Vial,Rx,Licensed,16-Jan-13,, ,,0,Original,,1795,001,CBER,16-Jan-13,,,16-Jan-25,,\r\n,Ferring Pharmaceuticals Inc.,17016,Novarel,chorionic gonadotropin,351(a),"20,000UNITS",For Injection,Intramuscular,Multi-Dose Vial,Disc,Licensed,15-Jan-74,,N/A,N/A,,Original,,2112,004,CDER,,,,,,\r\n'

PURPLE_BOOK_CSV_BYTES = PURPLE_BOOK_CSV.encode('utf-8')

#: Akamai bot-detection page: HTTP 404 with this body. Not a missing file.
BOT_DETECTION_BODY = (b"<!DOCTYPE html><html><head><title>FDA Apology</title>"
                      b"</head><body>abuse-detection-apology</body></html>")
