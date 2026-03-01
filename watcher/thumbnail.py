"""
Thumbnail Processor
Converts the ASIN-named thumbnail into a YouTube-ready 1280x720 image
with an AI-generated hook phrase overlay.

Pipeline:
1. Load and center-crop to 1280x720
2. Enhance brightness (1.2) and contrast (1.3)
3. Claude Vision: detect if a person/face is present
4. Claude API: generate 2-4 word hook phrase for the product
5. Analyze dominant color
6. Determine text color (white on dark / dark navy on light)
7. Add 70% opacity overlay strip (20% of image height)
8. Position: bottom if person detected, center if no person
9. Render hook phrase in Montserrat-ExtraBold at ~80% strip width
10. Save as thumbnail_final.jpg in the slot folder
"""

import base64
import io
import logging
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

log = logging.getLogger(__name__)

# --- Constants ---
THUMB_W = 1280
THUMB_H = 720
STRIP_HEIGHT_RATIO = 0.20          # Overlay strip = 20% of image height
OVERLAY_OPACITY = int(255 * 0.70)  # 70% opacity
TEXT_COLOR_LIGHT = "#ffffff"
TEXT_COLOR_DARK = "#1a1a2e"
TARGET_TEXT_WIDTH_RATIO = 0.80     # Hook phrase fills 80% of strip width

FONT_PATH = Path(__file__).parent.parent / "fonts" / "Montserrat-ExtraBold.ttf"


# --- Image helpers ---

def resize_and_crop(img: Image.Image) -> Image.Image:
    """Scale to cover 1280x720, then center crop."""
    img_ratio = img.width / img.height
    target_ratio = THUMB_W / THUMB_H

    if img_ratio > target_ratio:
        # Wider than target — scale by height
        new_h = THUMB_H
        new_w = int(img.width * (THUMB_H / img.height))
    else:
        # Taller than target — scale by width
        new_w = THUMB_W
        new_h = int(img.height * (THUMB_W / img.width))

    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - THUMB_W) // 2
    top = (new_h - THUMB_H) // 2
    return img.crop((left, top, left + THUMB_W, top + THUMB_H))


def enhance_image(img: Image.Image) -> Image.Image:
    img = ImageEnhance.Brightness(img).enhance(1.2)
    img = ImageEnhance.Contrast(img).enhance(1.3)
    img = ImageEnhance.Color(img).enhance(1.4)  # Boost saturation so colors pop
    return img


def dominant_color(img: Image.Image) -> tuple[int, int, int]:
    """Average RGB of a downscaled version of the image."""
    small = img.resize((50, 50), Image.LANCZOS).convert("RGB")
    pixels = list(small.getdata())
    r = sum(p[0] for p in pixels) // len(pixels)
    g = sum(p[1] for p in pixels) // len(pixels)
    b = sum(p[2] for p in pixels) // len(pixels)
    return (r, g, b)


def perceived_luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def image_to_base64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


# --- Claude API calls ---

def detect_person(img: Image.Image, client: anthropic.Anthropic) -> bool:
    """Return True if Claude detects a person or human face in the image."""
    image_b64 = image_to_base64(img)

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=5,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    }
                },
                {
                    "type": "text",
                    "text": "Does this image contain a person or human face? Reply with only YES or NO."
                }
            ]
        }]
    )

    answer = response.content[0].text.strip().upper()
    log.info(f"Person detection result: {answer}")
    return answer.startswith("YES")


def generate_hook_phrase(product_name: str, client: anthropic.Anthropic) -> str:
    """Generate a 2-4 word ALL CAPS hook phrase for the thumbnail."""
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=20,
        messages=[{
            "role": "user",
            "content": (
                f"Generate a 2-4 word hook phrase for a YouTube thumbnail for this product: {product_name}. "
                "Make it punchy, curiosity-driven, and suitable for a product review. "
                "Reply with only the hook phrase, no punctuation, all caps."
            )
        }]
    )

    phrase = response.content[0].text.strip().upper()
    log.info(f"Hook phrase generated: '{phrase}'")
    return phrase


# --- Text rendering ---

def fit_font(draw: ImageDraw.ImageDraw, text: str, target_width: int) -> ImageFont.FreeTypeFont:
    """Binary search for the largest font size where text fits within target_width."""
    if not FONT_PATH.exists():
        log.warning(f"Font not found at {FONT_PATH} — using PIL default font")
        return ImageFont.load_default()

    lo, hi = 10, 400
    best = ImageFont.truetype(str(FONT_PATH), lo)

    while lo <= hi:
        mid = (lo + hi) // 2
        font = ImageFont.truetype(str(FONT_PATH), mid)
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        if w <= target_width:
            best = font
            lo = mid + 1
        else:
            hi = mid - 1

    return best


# --- Overlay compositor ---

def make_gradient_strip(width: int, height: int, rgb: tuple, max_alpha: int) -> Image.Image:
    """
    Create a gradient strip that fades from transparent at the top
    to max_alpha at the bottom, using the given RGB color.
    """
    strip = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(strip)
    for y in range(height):
        alpha = int(max_alpha * (y / height))
        draw.line([(0, y), (width, y)], fill=(rgb[0], rgb[1], rgb[2], alpha))
    return strip


def add_overlay(img: Image.Image, hook_phrase: str, person_detected: bool) -> Image.Image:
    """Composite a gradient strip and hook phrase onto the image."""
    strip_h = int(THUMB_H * STRIP_HEIGHT_RATIO)

    # Always place the strip at the bottom (YouTube standard, most eye-catching)
    strip_y = THUMB_H - strip_h

    # Dark gradient from the bottom — white text always reads well on dark gradient
    text_color = TEXT_COLOR_LIGHT
    gradient = make_gradient_strip(THUMB_W, strip_h, (0, 0, 0), OVERLAY_OPACITY)

    img = img.convert("RGBA")
    img.paste(gradient, (0, strip_y), gradient)

    # Draw hook phrase with drop shadow for readability
    draw = ImageDraw.Draw(img)
    target_w = int(THUMB_W * TARGET_TEXT_WIDTH_RATIO)
    font = fit_font(draw, hook_phrase, target_w)

    bbox = draw.textbbox((0, 0), hook_phrase, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (THUMB_W - text_w) // 2
    text_y = strip_y + (strip_h - text_h) // 2

    # Drop shadow (offset 3px down-right, semi-transparent black)
    shadow_offset = max(3, font.size // 20)
    draw.text(
        (text_x + shadow_offset, text_y + shadow_offset),
        hook_phrase, font=font, fill=(0, 0, 0, 180)
    )

    # Main text
    draw.text((text_x, text_y), hook_phrase, font=font, fill=text_color)

    return img.convert("RGB")


# --- Public entry point ---

def process_thumbnail(
    image_file: Path,
    product_name: str,
    output_dir: Path,
    api_key: str
) -> Path | None:
    """
    Run the full thumbnail pipeline.
    Returns the path to thumbnail_final.jpg, or None if something fails.
    """
    client = anthropic.Anthropic(api_key=api_key)

    try:
        log.info(f"Processing thumbnail for: {product_name}")

        img = ImageOps.exif_transpose(Image.open(image_file)).convert("RGB")
        img = resize_and_crop(img)
        img = enhance_image(img)

        person_detected = detect_person(img, client)
        hook_phrase = generate_hook_phrase(product_name, client)

        img = add_overlay(img, hook_phrase, person_detected)

        output_path = output_dir / "thumbnail_final.jpg"
        img.save(output_path, format="JPEG", quality=92)
        log.info(f"Thumbnail saved: {output_path}")

        return output_path

    except Exception as e:
        log.error(f"Thumbnail processing failed for {image_file.name}: {e}")
        return None
