import fitz

doc = fitz.open("ss.pdf")
print("=" * 50)
print("Pages:", len(doc))
print("Can read:", not doc.is_closed)
print("Metadata:", doc.metadata)
print("=" * 50)

for i in [0, len(doc)//2, len(doc)-1]:
    try:
        page = doc.load_page(i)
        pix = page.get_pixmap(dpi=72)
        print(f"Page {i+1}: OK ({pix.width}x{pix.height})")
    except Exception as e:
        print(f"Page {i+1}: ERROR - {e}")

print("=" * 50)
print("PDF test completed successfully.")
