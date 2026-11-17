"""Builders for small test PDFs."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
from pikepdf import Dictionary, Name, OutlineItem, Pdf


def make_pdf(pages: int = 3, *, outline: bool = False, title: str | None = "Test document",
             image: bool = False, password: str | None = None, label: str = "Page") -> bytes:
    """A PDF whose page N contains the text '<label> N marker'."""
    pdf = Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    for number in range(1, pages + 1):
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        resources = Dictionary(Font=Dictionary(F1=font))
        content = f"BT /F1 36 Tf 72 700 Td ({label} {number} marker) Tj ET"
        if image and number == 1:
            from PIL import Image
            import random

            rng = random.Random(7)
            picture = Image.new("RGB", (1600, 1200))
            picture.putdata([(rng.randrange(256), (x * 7) % 256, (x // 1600) % 256) for x in range(1600 * 1200)])
            raw = picture.tobytes()
            xobject = pdf.make_stream(raw, Type=Name.XObject, Subtype=Name.Image, Width=1600, Height=1200,
                                      ColorSpace=Name.DeviceRGB, BitsPerComponent=8)
            resources.XObject = Dictionary(Im1=xobject)
            content += " q 400 0 0 300 72 300 cm /Im1 Do Q"
        page.obj.Resources = resources
        page.obj.Contents = pdf.make_stream(content.encode())
    if outline:
        with pdf.open_outline() as root:
            for index in range(pages):
                root.root.append(OutlineItem(f"Chapter {index + 1}", index))
    if title:
        pdf.docinfo["/Title"] = title
        pdf.docinfo["/Author"] = "ARD tests"
        pdf.docinfo["/CreationDate"] = "D:20240131120000+05'30'"
    buffer = io.BytesIO()
    if password:
        pdf.save(buffer, encryption=pikepdf.Encryption(owner=password, user=password))
    else:
        pdf.save(buffer)
    return buffer.getvalue()


def page_texts(path: Path | str) -> list[str]:
    import pypdf

    return [(page.extract_text() or "").strip() for page in pypdf.PdfReader(str(path)).pages]


def make_form_pdf() -> bytes:
    """A one-page PDF with a text field, a multiline field, a read-only field, a checkbox, a radio group and a dropdown."""
    pdf = Pdf.open(io.BytesIO(make_pdf(1)))
    page = pdf.pages[0]
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                        Encoding=Name.WinAnsiEncoding))
    appearance = pikepdf.String("/Helv 11 Tf 0 g")

    def widget(**entries):
        return pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, P=page.obj, F=4, **entries))

    def button_states(on_name: str):
        on = pdf.make_stream(b"q 0 0 0 rg 2 2 8 8 re f Q", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 12, 12])
        off = pdf.make_stream(b"", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, 12, 12])
        return Dictionary(N=Dictionary({"/" + on_name: on, "/Off": off}))

    full_name = widget(FT=Name.Tx, T=pikepdf.String("full_name"), TU=pikepdf.String("Full name"),
                       Rect=[72, 600, 300, 620], DA=appearance, Ff=2)                       # required
    notes = widget(FT=Name.Tx, T=pikepdf.String("notes"), Rect=[72, 500, 300, 580], DA=appearance, Ff=1 << 12)
    reference = widget(FT=Name.Tx, T=pikepdf.String("reference"), Rect=[320, 600, 500, 620], DA=appearance, Ff=1,
                       V=pikepdf.String("REF-001"))                                          # read-only
    agree = widget(FT=Name.Btn, T=pikepdf.String("agree"), Rect=[72, 460, 84, 472], V=Name.Off, AS=Name.Off,
                   AP=button_states("Yes"))
    country = widget(FT=Name.Ch, T=pikepdf.String("country"), Rect=[72, 420, 200, 440], DA=appearance, Ff=1 << 17,
                     Opt=pikepdf.Array([pikepdf.String("India"), pikepdf.String("Nepal"), pikepdf.String("Bhutan")]))
    plan = pdf.make_indirect(Dictionary(FT=Name.Btn, T=pikepdf.String("plan"), Ff=(1 << 15) | (1 << 14), V=Name.Off))
    basic = widget(Parent=plan, Rect=[72, 380, 84, 392], AS=Name.Off, AP=button_states("basic"))
    pro = widget(Parent=plan, Rect=[100, 380, 112, 392], AS=Name.Off, AP=button_states("pro"))
    plan.Kids = pikepdf.Array([basic, pro])

    page.obj.Annots = pikepdf.Array([full_name, notes, reference, agree, country, basic, pro])
    pdf.Root.AcroForm = Dictionary(Fields=pikepdf.Array([full_name, notes, reference, agree, country, plan]),
                                   DR=Dictionary(Font=Dictionary(Helv=font)), DA=appearance)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def make_p12(common_name: str = "ARD Test Signer", passphrase: bytes = b"test-pass", *, expired: bool = False) -> bytes:
    """A self-signed certificate and key as PKCS#12, for signature tests only."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ARD tests")])
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=800 if expired else 1)
    end = now - datetime.timedelta(days=400) if expired else now + datetime.timedelta(days=365)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(b"ard-test", key, certificate, None,
                                                 serialization.BestAvailableEncryption(passphrase))


def make_scanned_pdf(lines_per_page: list[list[str]], *, font_path: str | None = None, dpi: int = 200) -> bytes:
    """Image-only pages (as a scanner produces): the text exists only as pixels."""
    from PIL import Image, ImageDraw, ImageFont

    pdf = Pdf.new()
    width, height = int(8.5 * dpi), int(11 * dpi)
    for lines in lines_per_page:
        image = Image.new("L", (width, height), 255)
        draw = ImageDraw.Draw(image)
        size = int(dpi * 0.2)
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default(size=size)
        for index, line in enumerate(lines):
            draw.text((dpi, dpi + index * int(size * 1.7)), line, fill=0, font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        picture = pdf.make_stream(b"")
        picture.write(buffer.getvalue(), filter=Name.DCTDecode)
        picture.Type, picture.Subtype = Name.XObject, Name.Image
        picture.Width, picture.Height = width, height
        picture.ColorSpace, picture.BitsPerComponent = Name.DeviceGray, 8
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.obj.Resources = Dictionary(XObject=Dictionary(Im0=picture))
        page.obj.Contents = pdf.make_stream(b"q 612 0 0 792 0 0 cm /Im0 Do Q")
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()
