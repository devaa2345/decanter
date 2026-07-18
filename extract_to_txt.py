import fitz
import os

pdfs = [
    r"Sovereign Scents - Decant Sheet - Google Sheets.pdf",
    r"Sovereign Scents - Decant Sheet - Google Sheets2.pdf",
    r"Sovereign Scents - Decant Sheet - Google sheets3.pdf",
]

with open("extracted_catalog.txt", "w", encoding="utf-8") as out:
    for pdf_path in pdfs:
        full_path = os.path.join(r"c:\Users\ABC\Downloads\decanter", pdf_path)
        out.write(f"\n{'='*80}\n")
        out.write(f"FILE: {pdf_path}\n")
        out.write(f"{'='*80}\n")
        doc = fitz.open(full_path)
        for i, page in enumerate(doc):
            out.write(f"\n--- Page {i+1} ---\n")
            out.write(page.get_text())
        doc.close()

print("Extracted all PDFs successfully to extracted_catalog.txt")
