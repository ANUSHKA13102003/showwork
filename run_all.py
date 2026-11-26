from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app import db
from evals import run as run_evals
from pipeline import build

ROOT = Path(__file__).resolve().parent
SOCIAL_PNG = ROOT / "docs" / "social.png"


def _load_font(size: int, bold: bool = False):
    candidates = (
        ["arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def generate_social_card():
    SOCIAL_PNG.parent.mkdir(parents=True, exist_ok=True)
    w, h = 1280, 640
    img = Image.new("RGB", (w, h), "#FAF9F6")
    draw = ImageDraw.Draw(img)

    # Subtle frame and top accent rule (single accent color, no gradients)
    draw.rectangle([36, 36, w - 36, h - 36], outline="#E4E1D8", width=2)
    draw.rectangle([36, 36, w - 36, 48], fill="#0F5E4C")

    f_eyebrow = _load_font(22, bold=True)
    f_title = _load_font(78, bold=True)
    f_tag = _load_font(36, bold=False)
    f_meta = _load_font(24, bold=False)

    draw.text((96, 120), "SEMANTIC-LAYER ANALYTICS COPILOT", fill="#0F5E4C", font=f_eyebrow)
    draw.text((96, 175), "Showwork", fill="#181816", font=f_title)
    draw.text((96, 285), "Ask your data anything. It shows its work.", fill="#181816", font=f_tag)
    draw.text(
        (96, 365),
        "Governed metrics · Read-only DuckDB · Measured accuracy · Tested refusals",
        fill="#5C5B55",
        font=f_meta,
    )

    # Bottom pill strip
    pills = ["Definition + SQL", "Exact row count", "95% Wilson CI evals", "Zero LLM math"]
    x = 96
    y = 485
    f_pill = _load_font(21, bold=True)
    for p in pills:
        bbox = draw.textbbox((0, 0), p, font=f_pill)
        pw = (bbox[2] - bbox[0]) + 36
        draw.rounded_rectangle([x, y, x + pw, y + 48], radius=8, fill="#E6F2EF", outline="#B7DDD4", width=1)
        draw.text((x + 18, y + 11), p, fill="#0F5E4C", font=f_pill)
        x += pw + 16

    img.save(SOCIAL_PNG, format="PNG", optimize=True)


def main():
    print("1/3 Running data pipeline and semantic metadata build...")
    build.build(use_llm=True)
    db.semantic.cache_clear()
    db.meta.cache_clear()

    print("2/3 Generating social preview card...")
    generate_social_card()

    print("3/3 Running evaluation suite...")
    run_evals.evaluate(only_split=None, refresh=False)
    print("Done. Start the server with: uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
