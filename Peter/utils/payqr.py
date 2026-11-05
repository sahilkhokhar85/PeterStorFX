"""Payment card image: the owner's QR template with a fresh QR (fixed amount) for each order."""
import io
import os

from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(_HERE, "..", "assets", "pay_template.jpg")

# measured on the 1932x1932 template
QR_BOX = (410, 648, 1530, 1770)          # where the QR sits
LOGO_BOX = (892, 1130, 1052, 1292)       # the round logo in the middle of the original QR
TEXT_BOX = (440, 500, 1560, 625)         # where the UPI id was printed (now the amount)
FONTS = [
    os.path.join(_HERE, "..", "assets", "Poppins-Bold.ttf"),   # shipped with the bot (the slim Docker image has no fonts)
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]


def _font(size):
    for path in FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _matrix(data):
    """QR modules (True = dark), error correction H, without the quiet border."""
    import qrcode
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def render(matrix, label):
    img = Image.open(TEMPLATE).convert("RGB")
    logo = img.crop(LOGO_BOX)                       # keep the original logo
    draw = ImageDraw.Draw(img)

    # amount instead of the UPI id
    draw.rectangle(TEXT_BOX, fill="white")
    font = _font(100)
    # baseline where the UPI id's baseline was, so the text sits clear of the QR
    draw.text(((TEXT_BOX[0] + TEXT_BOX[2]) / 2, 590), label, font=font, fill="black", anchor="ms")

    # new QR, same place and size as the old one
    n = len(matrix)
    qr_img = Image.new("1", (n, n), 1)
    qr_img.putdata([0 if cell else 1 for row in matrix for cell in row])
    size = (QR_BOX[2] - QR_BOX[0], QR_BOX[3] - QR_BOX[1])
    img.paste(qr_img.convert("RGB").resize(size, Image.NEAREST), QR_BOX[:2])

    # logo back in the middle (round)
    mask = Image.new("L", logo.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, logo.size[0] - 1, logo.size[1] - 1), fill=255)
    cx = (QR_BOX[0] + QR_BOX[2]) // 2 - logo.size[0] // 2
    cy = (QR_BOX[1] + QR_BOX[3]) // 2 - logo.size[1] // 2
    img.paste(logo, (cx, cy), mask)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    out.seek(0)
    out.name = "pay.jpg"
    return out


def make_card(upi_uri, label):
    """upi_uri: the upi://pay?... link (fixed amount). label: text shown on top, e.g. '₹49'."""
    return render(_matrix(upi_uri), label)
