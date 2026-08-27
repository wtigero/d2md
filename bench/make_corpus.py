"""Render a scanned-document corpus with exact ground truth for benchmark scripts.

Real scanned documents are the honest test, but none of the ones on this
machine can be published — they are client-confidential. So the controlled half
of the corpus is synthesised here: text we wrote, rendered through the system's
native fonts for each script, flattened to an image and wrapped in a PDF with
no text layer. That is what an OCR engine sees from a scan, and unlike a real
scan we know exactly what the answer is, which is what makes CER meaningful.

Two variants per script:

    clean   straight render, the easy case
    noisy   rotated, blurred, speckled, contrast-reduced — a photocopy

Complex-script shaping matters here. Thai stacks vowels above and below the
consonant and other complex scripts form conjuncts; without HarfBuzz the
render is already wrong before OCR sees it, and we would be measuring our own
bug. The script uses Pillow RAQM when present and falls back to Pango's
HarfBuzz renderer when it is not.

    python bench/make_corpus.py OUTDIR
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Two faces per script where the system has them. A serif and a sans differ
# enough in stroke weight and counter shape to catch an engine that has overfit
# to one, and Thai in particular is read very differently in a looped face than
# in a loopless one.
FONTS = {
    "en": [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    "th": [
        "/System/Library/Fonts/Supplemental/Ayuthaya.ttf",
        "/System/Library/Fonts/Supplemental/Thonburi.ttc",
    ],
    "ja": [
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
    ],
    "zh": [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ],
    "ko": [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/System/Library/Fonts/Supplemental/AppleMyungjo.ttf",
    ],
    # Second wave. Vietnamese and German are Latin script, which is the point:
    # they test whether a detector built on Unicode blocks can tell languages
    # apart at all when the block is the same.
    "vi": [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    "de": [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    "ru": [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    # GeezaPro renders the Arabic correctly but has no ASCII digits — every
    # policy and phone number came out as tofu boxes, which would have measured
    # the corpus rather than the engine. Arial Unicode carries both.
    "ar": [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ],
    # Traditional Chinese; `zh` above is Simplified. Songti was the obvious
    # second face and is missing 19 of these characters — it covers Simplified
    # well and Traditional badly, which is exactly the trap the glyph check
    # now catches.
    "zt": [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
    ],
}

FALLBACK_FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

# Pillow built without RAQM cannot shape Thai, Arabic, or Indic scripts. Pango
# uses HarfBuzz through the OS stack and is available on the macOS preparation
# machine, so it is a safe fallback for creating the one corpus that is then
# copied unchanged to the other benchmark hosts.
PANGO_FONTS = {
    "en": ["Arial", "Times New Roman"],
    "th": ["Thonburi", "Ayuthaya"],
    "ja": ["Hiragino Sans", "Hiragino Mincho Pro"],
    "zh": ["Hiragino Sans GB", "Songti SC"],
    "ko": ["Apple SD Gothic Neo", "AppleMyungjo"],
    "vi": ["Arial", "Times New Roman"],
    "de": ["Arial", "Times New Roman"],
    "ru": ["Arial", "Times New Roman"],
    "ar": ["Geeza Pro", "Al Nile"],
    "zt": ["Hiragino Sans", "Hiragino Mincho Pro"],
}
PANGO_LANGUAGES = {
    "en": "en_US", "th": "th_TH", "ja": "ja_JP", "zh": "zh_CN",
    "ko": "ko_KR", "vi": "vi_VN", "de": "de_DE",
    "ru": "ru_RU", "ar": "ar_SA", "zt": "zh_TW",
}

# Deliberately ordinary business prose — the register d2md actually meets —
# with digits and punctuation mixed in, because those are where OCR trained on
# one script tends to drift into another's glyph set.
TEXT = {
    "en": [
        "Personal Accident Insurance Policy Summary",
        "Policy number 4471-9920-118 was issued on 3 March 2026.",
        "The annual premium is 12,450 baht, payable in four instalments.",
        "Coverage begins at 00:01 on the day following approval.",
        "Claims must be filed within 30 days of the incident date.",
        "Contact the service centre at 02-116-4400 for assistance.",
    ],
    "th": [
        "สรุปกรมธรรม์ประกันอุบัติเหตุส่วนบุคคล",
        "กรมธรรม์เลขที่ 4471-9920-118 ออกให้เมื่อวันที่ 3 มีนาคม 2569",
        "เบี้ยประกันภัยรายปีจำนวน 12,450 บาท ชำระได้สี่งวด",
        "ความคุ้มครองเริ่มต้นเวลา 00:01 น. ของวันถัดจากวันอนุมัติ",
        "ผู้เอาประกันภัยต้องยื่นเรื่องภายใน 30 วันนับจากวันเกิดเหตุ",
        "ติดต่อศูนย์บริการลูกค้าได้ที่หมายเลข 02-116-4400",
    ],
    "ja": [
        "個人傷害保険契約の概要",
        "証券番号 4471-9920-118 は 2026 年 3 月 3 日に発行されました。",
        "年間保険料は 12,450 バーツで、四回に分けて支払います。",
        "補償は承認日の翌日午前 0 時 1 分から開始します。",
        "保険金の請求は事故日から 30 日以内に行ってください。",
        "お問い合わせは 02-116-4400 までご連絡ください。",
    ],
    "zh": [
        "个人意外伤害保险合同摘要",
        "保单号码 4471-9920-118 于 2026 年 3 月 3 日签发。",
        "年度保费为 12,450 泰铢，可分四期支付。",
        "保障自批准次日零时一分起生效。",
        "理赔申请须在事故发生之日起 30 日内提交。",
        "如需协助请致电客服中心 02-116-4400。",
    ],
    "ko": [
        "개인 상해 보험 계약 요약",
        "증권 번호 4471-9920-118 은 2026 년 3 월 3 일에 발행되었습니다.",
        "연간 보험료는 12,450 바트이며 네 번에 나누어 납부합니다.",
        "보장은 승인일 다음 날 오전 0 시 1 분부터 시작됩니다.",
        "보험금 청구는 사고일로부터 30 일 이내에 제출해야 합니다.",
        "문의 사항은 고객 센터 02-116-4400 으로 연락하십시오.",
    ],
    "vi": [
        "Tóm tắt hợp đồng bảo hiểm tai nạn cá nhân",
        "Số hợp đồng 4471-9920-118 được cấp ngày 3 tháng 3 năm 2026.",
        "Phí bảo hiểm hàng năm là 12.450 baht, trả thành bốn đợt.",
        "Bảo hiểm có hiệu lực từ 00:01 ngày kế tiếp sau khi được duyệt.",
        "Yêu cầu bồi thường phải nộp trong vòng 30 ngày kể từ ngày xảy ra.",
        "Liên hệ trung tâm dịch vụ khách hàng số 02-116-4400.",
    ],
    "de": [
        "Zusammenfassung der privaten Unfallversicherung",
        "Die Police 4471-9920-118 wurde am 3. März 2026 ausgestellt.",
        "Der Jahresbeitrag beträgt 12.450 Baht, zahlbar in vier Raten.",
        "Der Versicherungsschutz beginnt um 00:01 Uhr am Folgetag.",
        "Ansprüche sind innerhalb von 30 Tagen nach dem Ereignis zu melden.",
        "Wenden Sie sich an das Servicecenter unter 02-116-4400.",
    ],
    "ru": [
        "Краткое изложение договора личного страхования",
        "Полис номер 4471-9920-118 выдан 3 марта 2026 года.",
        "Годовой взнос составляет 12 450 бат и выплачивается в четыре срока.",
        "Покрытие начинается в 00:01 на следующий день после одобрения.",
        "Заявление подаётся в течение 30 дней с даты происшествия.",
        "Обращайтесь в сервисный центр по телефону 02-116-4400.",
    ],
    "ar": [
        "ملخص وثيقة التأمين ضد الحوادث الشخصية",
        "تم إصدار الوثيقة رقم 4471-9920-118 بتاريخ 3 مارس 2026.",
        "القسط السنوي هو 12,450 بات ويدفع على أربع دفعات.",
        "تبدأ التغطية الساعة 00:01 من اليوم التالي للموافقة.",
        "يجب تقديم المطالبة خلال 30 يوما من تاريخ الحادث.",
        "للتواصل مع مركز الخدمة على الرقم 02-116-4400.",
    ],
    "zt": [
        "個人意外傷害保險合約摘要",
        "保單號碼 4471-9920-118 於 2026 年 3 月 3 日簽發。",
        "年度保費為 12,450 泰銖，可分四期支付。",
        "保障自核准次日零時一分起生效。",
        "理賠申請須於事故發生之日起 30 日內提交。",
        "如需協助請致電客服中心 02-116-4400。",
    ],
}

# Real documents are rarely monolingual. A Thai contract quotes English product
# names; a Japanese invoice carries a romanised address. Script detection
# assumes one language per page, and these are the documents that break that
# assumption — so they are in the corpus rather than in a caveat.
#
# Keyed by the script the detector ought to pick: the one carrying the meaning,
# not necessarily the one with the most characters.
MIXED = {
    "th": [
        "ใบเสนอราคา / Quotation",
        "ผลิตภัณฑ์ Cloud Storage Premium จำนวน 3 licence",
        "ราคารวม 45,000 บาท (VAT included) กำหนดส่งมอบ 15 วัน",
        "ติดต่อ Sales Department โทร 02-116-4400 ต่อ 220",
        "เงื่อนไขการชำระเงิน Net 30 นับจากวันที่ออกใบแจ้งหนี้",
    ],
    "ja": [
        "請求書 / Invoice No. INV-2026-0042",
        "株式会社サンプル Sample Corporation 御中",
        "Cloud Storage Premium ライセンス 3 件 45,000 円",
        "お支払い期限 Net 30 days 銀行振込にてお願いします",
        "担当 Sales Department 電話 03-1234-5678",
    ],
}

# 200 DPI on A4 — the low end of what an office scanner produces, so the
# numbers are not flattered by resolution.
W, H = 1654, 2339
MARGIN = 160
SIZE = 44
LEADING = 82


def _font(lang: str, size: int, face: int = 0):
    from PIL import ImageFont

    choices = FONTS[lang]
    path = choices[face] if face < len(choices) else choices[0]
    if not Path(path).exists():
        path = choices[0] if Path(choices[0]).exists() else FALLBACK_FONT
    return ImageFont.truetype(path, size)


def _has_raqm() -> bool:
    from PIL import features

    return bool(features.check("raqm"))


def _render_pango(lang: str, lines: list[str], face: int = 0):
    """Render shaped text through Pango when Pillow lacks RAQM support."""
    from PIL import Image

    family = PANGO_FONTS[lang][face % len(PANGO_FONTS[lang])]
    with tempfile.TemporaryDirectory(prefix="d2md-corpus-") as temporary:
        output = Path(temporary) / "page.png"
        command = [
            "pango-view",
            "--backend=cairo",
            "--no-display",
            "--pixels",
            "--background=white",
            "--foreground=black",
            f"--margin={MARGIN}",
            f"--width={W - 2 * MARGIN}",
            f"--font={family} {SIZE}",
            f"--language={PANGO_LANGUAGES[lang]}",
            f"--output={output}",
            "--text=" + "\n".join(lines),
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0 or not output.is_file():
            detail = completed.stderr.strip() or "no image was written"
            raise SystemExit(f"Pango could not render {lang}: {detail}")
        with Image.open(output) as rendered:
            image = rendered.convert("L")
        if image.width > W or image.height > H:
            raise SystemExit(
                f"Pango rendered {lang} outside the fixed corpus page size"
            )
        page = Image.new("L", (W, H), 255)
        page.paste(image, (0, 0))
        return page


def _missing_glyphs(font, text: str) -> list[str]:
    """Characters this font has no glyph for.

    Three separate corpus bugs have been this: GeezaPro has no ASCII digits, so
    every Arabic policy number rendered as tofu; Songti is missing most
    Traditional Chinese, so half that page came out blank and both OCR engines
    were charged for it. A blank page region scores as a misread, and the
    number then describes the font rather than the engine.

    FreeType substitutes .notdef for a missing glyph, which renders either
    empty or as a box. Comparing each character's mask against the mask of a
    codepoint no font defines catches both shapes without needing fontTools.
    """
    reference = font.getmask("￾")  # a permanent noncharacter
    ref = bytes(reference)
    missing = []
    for ch in dict.fromkeys(text):
        if ch.isspace():
            continue
        mask = bytes(font.getmask(ch))
        if not mask or mask == ref:
            missing.append(ch)
    return missing


def _render(lang: str, lines: list[str], face: int = 0):
    if not _has_raqm():
        if shutil.which("pango-view") is None:
            raise SystemExit(
                "Pillow has no RAQM/HarfBuzz support and pango-view is not "
                "available. Install one of them before generating a corpus."
            )
        return _render_pango(lang, lines, face)

    from PIL import Image, ImageDraw

    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    font = _font(lang, SIZE, face)
    heading = _font(lang, int(SIZE * 1.35), face)

    gaps = _missing_glyphs(font, "".join(lines))
    if gaps:
        raise SystemExit(
            f"{lang} face {face} ({FONTS[lang][face]}) has no glyph for "
            f"{''.join(gaps)!r}. Those characters would render blank and be "
            f"scored as OCR errors. Pick a font that covers the text."
        )

    y = MARGIN
    for i, line in enumerate(lines):
        draw.text((MARGIN, y), line, font=heading if i == 0 else font, fill=20)
        y += int(LEADING * 1.5) if i == 0 else LEADING
    return img


def _degrade(img):
    """Make it look photocopied rather than printed.

    Skew, blur and speckle are what actually separate OCR engines; a clean
    render flatters everything equally.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    img = img.rotate(0.7, resample=Image.BICUBIC, fillcolor=255, expand=False)
    img = img.filter(ImageFilter.GaussianBlur(0.8))

    arr = np.asarray(img).astype(np.int16)
    rng = np.random.default_rng(20260811)  # fixed: the corpus must be stable
    arr = arr + rng.normal(0, 9, arr.shape)
    arr = 20 + arr * 0.86  # lift the blacks, drop the whites
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _to_pdf(img, dst: Path):
    """Wrap the image in a PDF with no text layer at all — a scan."""
    img.convert("RGB").save(dst, "PDF", resolution=200.0)


def main(argv: list[str]) -> int:
    outdir = Path(argv[1] if len(argv) > 1 else "corpus").expanduser()
    (outdir / "pdf").mkdir(parents=True, exist_ok=True)
    (outdir / "truth").mkdir(parents=True, exist_ok=True)

    def emit(name: str, img, lines: list[str]) -> None:
        _to_pdf(img, outdir / "pdf" / f"{name}.pdf")
        (outdir / "truth" / f"{name}.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        print(f"  {name}.pdf")

    count = 0
    for lang, lines in TEXT.items():
        # Face 0 gets both a clean and a photocopied variant; face 1 gets the
        # clean one only. The point of the second face is to catch an engine
        # that has learned one typeface, and degrading it as well would double
        # the corpus without asking a new question.
        clean = _render(lang, lines, face=0)
        emit(f"{lang}-clean", clean, lines)
        emit(f"{lang}-noisy", _degrade(clean), lines)
        emit(f"{lang}-alt", _render(lang, lines, face=1), lines)
        count += 3

    for lang, lines in MIXED.items():
        mixed = _render(lang, lines, face=0)
        emit(f"{lang}-mixed", mixed, lines)
        emit(f"{lang}-mixednoisy", _degrade(mixed), lines)
        count += 2

    print(f"\n{count} documents → {outdir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
