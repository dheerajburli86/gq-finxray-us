from pypdf import PdfReader, PdfWriter
import os

INPUT_FILE = "ss.pdf"
OUTPUT_DIR = "Split_PDFs"
PAGES_PER_PART = 500

os.makedirs(OUTPUT_DIR, exist_ok=True)

reader = PdfReader(INPUT_FILE)
total_pages = len(reader.pages)

print(f"Total pages: {total_pages}")

part = 1

for start in range(0, total_pages, PAGES_PER_PART):
    writer = PdfWriter()

    end = min(start + PAGES_PER_PART, total_pages)

    for page_num in range(start, end):
        writer.add_page(reader.pages[page_num])

    output_file = os.path.join(
        OUTPUT_DIR,
        f"ss_part_{part}_pages_{start+1}_to_{end}.pdf"
    )

    with open(output_file, "wb") as f:
        writer.write(f)

    print(f"Created: {output_file}")

    part += 1

print("Done!")
