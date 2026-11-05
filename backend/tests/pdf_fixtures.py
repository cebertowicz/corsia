"""Small PDFs built byte by byte for tests: text, pictures, vector shapes and filled-in form fields."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Page:
    """One page: its text, whether it draws a picture, how many rectangles it draws, and form-field values."""

    text: str = ""
    image: bool = False
    shapes: int = 0
    fields: tuple[str, ...] = ()


def _page_stream(page: Page) -> bytes:
    parts = []
    if page.text:
        parts.append(f"BT /F1 12 Tf 20 150 Td ({page.text}) Tj ET")
    if page.image:
        parts.append("q 60 0 0 60 20 20 cm /Im1 Do Q")
    parts.extend(f"{(i % 20) * 10} {(i // 20) * 10} 8 8 re f" for i in range(page.shapes))
    return "\n".join(parts).encode()


def build(pages: Sequence[Page]) -> bytes:
    """A PDF with these pages; any page with ``fields`` makes the PDF a form."""
    first_page = 5
    objects: dict[int, bytes] = {}
    kids, widgets, next_number = [], [], first_page + 2 * len(pages)
    for index, page in enumerate(pages):
        number, stream = first_page + 2 * index, _page_stream(page)
        annots = []
        for value in page.fields:
            objects[next_number] = (
                f"<< /Type /Annot /Subtype /Widget /FT /Tx /T (field{next_number}) /V ({value}) "
                f"/Rect [20 20 220 40] /P {number} 0 R /F 4 /DA (/F1 12 Tf 0 g) >>"
            ).encode()
            annots.append(next_number)
            next_number += 1
        widgets += annots
        listed = " ".join(f"{n} 0 R" for n in annots)
        objects[number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents {number + 1} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R >> >> /Annots [{listed}] >>"
        ).encode()
        objects[number + 1] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        kids.append(number)
    form = ""
    if widgets:
        refs = " ".join(f"{n} 0 R" for n in widgets)
        form = f" /AcroForm << /Fields [{refs}] /DA (/F1 12 Tf 0 g) /DR << /Font << /F1 3 0 R >> >> >>"
    objects[1] = f"<< /Type /Catalog /Pages 2 0 R{form} >>".encode()
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objects[4] = (b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB "
                  b"/BitsPerComponent 8 /Length 3 >>\nstream\n\xff\x00\x00\nendstream")
    out, offsets = b"%PDF-1.4\n", {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    size, xref = max(objects) + 1, len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    out += b"".join(b"%010d 00000 n \n" % offsets[n] for n in range(1, size))
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)


def text_pdf(*texts: str) -> bytes:
    """A PDF with one page per text."""
    return build([Page(text=text) for text in texts])


# One page of text, locked against opening (AES-256), made once with a PDF library.
LOCKED_PDF = base64.b64decode(
    "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovVHlwZSAvQ2F0YWxvZwovUGFnZXMgMiAwIFIKPj4KZW5kb2JqCjIgMCBvYmoKPDwKL1R5cGUgL1"
    "BhZ2VzCi9LaWRzIFsgMyAwIFIgXQovQ291bnQgMQo+PgplbmRvYmoKMyAwIG9iago8PAovVHlwZSAvUGFnZQovUGFyZW50IDIgMCBSCi9NZWRp"
    "YUJveCBbIDAgMCAzMDAgMjAwIF0KL0NvbnRlbnRzIDQgMCBSCi9SZXNvdXJjZXMgPDwKL0ZvbnQgPDwKL0YxIDUgMCBSCj4+Ci9YT2JqZWN0ID"
    "w8Ci9JbTEgNiAwIFIKPj4KPj4KL0Fubm90cyBbIF0KPj4KZW5kb2JqCjQgMCBvYmoKPDwKL0xlbmd0aCA2NAo+PgpzdHJlYW0K+3jPz0dYUbnI"
    "JR4Njsi8E61Zdmbpo8EzGm4NxrprD88FFp1LzixVNmxUvmlpBXZqKr2VSbxYjMUOI6xkEtxJegplbmRzdHJlYW0KZW5kb2JqCjUgMCBvYmoKPD"
    "wKL1R5cGUgL0ZvbnQKL1N1YnR5cGUgL1R5cGUxCi9CYXNlRm9udCAvSGVsdmV0aWNhCj4+CmVuZG9iago2IDAgb2JqCjw8Ci9UeXBlIC9YT2Jq"
    "ZWN0Ci9TdWJ0eXBlIC9JbWFnZQovV2lkdGggMQovSGVpZ2h0IDEKL0NvbG9yU3BhY2UgL0RldmljZVJHQgovQml0c1BlckNvbXBvbmVudCA4Ci"
    "9MZW5ndGggMzIKPj4Kc3RyZWFtCspg2G54z/4UDX80LIvzsSoLbuejUn/eC31Tnpwvgqj6CmVuZHN0cmVhbQplbmRvYmoKNyAwIG9iago8PAov"
    "ViA1Ci9SIDYKL0xlbmd0aCAyNTYKL1AgNDI5NDk2NzI5MgovRmlsdGVyIC9TdGFuZGFyZAovTyA8YTZmOWI5NWMzZWI1NTI0ZjMzY2VkYmI2ZW"
    "EwYzM0YjU2MGY0MzE3YzhlYzE1OTNmMjRmNTExY2E0MDg1MTY4OWFhMjk3NGNjMmZjMTBhNmVkNTFhN2EzYmU4ZTZlZTZmPgovVSA8OTI3M2Zk"
    "MjY0NTE3ZjE1Yjc5ZjJiZjJlMmZjZWQwZjYyY2FkNzZjYmViMDU3NDljODA1YTIyZTk0OTg2MDFmNjEzMmQyMzEzMWI2MGFmOWYyYThmYmNiMT"
    "E1NDc3MDUyPgovQ0YgPDwKL1N0ZENGIDw8Ci9BdXRoRXZlbnQgL0RvY09wZW4KL0NGTSAvQUVTVjMKL0xlbmd0aCAzMgo+Pgo+PgovU3RtRiAv"
    "U3RkQ0YKL1N0ckYgL1N0ZENGCi9PRSA8YmUyYjMxZGUyMTFmNDg1ZGY4OTBmMmIwYTE0MDRiNzBhZjM1MzVkMTI2OTVlYjI0MmJjYTM2OTQ0ZT"
    "U1Y2Y3Yz4KL1VFIDw5Y2Y4NzRmODczZTczZjM1MzRjYWExNGI5Y2JlMDNiZDdiNTRhOGVmZmZlNmU2ZjU0ZTlhYWJkN2ZmYzE2N2M1PgovUGVy"
    "bXMgPDczYmRkMzdlZmE2ODEzMThlNzhmMTBlYjNkMWJhMDA1Pgo+PgplbmRvYmoKeHJlZgowIDgKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAwMD"
    "AwMDE1IDAwMDAwIG4gCjAwMDAwMDAwNjQgMDAwMDAgbiAKMDAwMDAwMDEyMyAwMDAwMCBuIAowMDAwMDAwMjg5IDAwMDAwIG4gCjAwMDAwMDA0"
    "MDMgMDAwMDAgbiAKMDAwMDAwMDQ3MyAwMDAwMCBuIAowMDAwMDAwNjQ4IDAwMDAwIG4gCnRyYWlsZXIKPDwKL1NpemUgOAovUm9vdCAxIDAgUg"
    "ovSUQgWyA8NjMzMjM3NjMzOTM1NjEzMjY1MzkzMjY1NjIzNTYyMzY2NjYzMzEzNTM4Mzk2MjMxMzkzNjM4NjM2NjMzMzUzNj4gPDYzMzIzNzYz"
    "MzkzNTYxMzI2NTM5MzI2NTYyMzU2MjM2NjY2MzMxMzUzODM5NjIzMTM5MzYzODYzNjYzMzM1MzY+IF0KL0VuY3J5cHQgNyAwIFIKPj4Kc3Rhcn"
    "R4cmVmCjEyMDMKJSVFT0YK"
)
