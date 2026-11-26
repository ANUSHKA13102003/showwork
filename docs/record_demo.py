import argparse
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

OUT_GIF = Path(__file__).resolve().parent / "demo.gif"
TARGET_WIDTH = 900


def _caption_font(size: int = 16):
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def add_frame(frames: list[Image.Image], durations: list[int], png_bytes: bytes, caption: str, duration_ms: int):
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    ratio = TARGET_WIDTH / img.width
    resized = img.resize((TARGET_WIDTH, round(img.height * ratio)), Image.Resampling.LANCZOS)

    # Subtle bottom caption bar so hiring managers follow each step in 12-15s
    draw = ImageDraw.Draw(resized)
    bar_h = 34
    y0 = resized.height - bar_h
    draw.rectangle([0, y0, resized.width, resized.height], fill="#181816")
    draw.rectangle([0, y0, 6, resized.height], fill="#0F5E4C")
    draw.text((16, y0 + 8), caption, fill="#FAF9F6", font=_caption_font(15))

    frames.append(resized)
    durations.append(duration_ms)


def _launch_browser(p):
    for ch in ("msedge", "chrome", None):
        try:
            return p.chromium.launch(channel=ch, headless=True) if ch else p.chromium.launch(headless=True)
        except PlaywrightError:
            continue
    raise RuntimeError("No Chromium/Edge/Chrome browser available for Playwright.")


def record(base_url: str):
    url = base_url.rstrip("/")
    frames: list[Image.Image] = []
    durations: list[int] = []

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(viewport={"width": 1240, "height": 800}, device_scale_factor=1)
        page = context.new_page()

        # 1. Open Briefing and wait for KPIs + chart to settle (total ~13.5 seconds across 7 frames)
        page.goto(f"{url}/briefing", wait_until="networkidle", timeout=90000)
        page.wait_for_selector("#briefing-kpis .kpi-btn", timeout=60000)
        page.wait_for_timeout(700)
        add_frame(
            frames,
            durations,
            page.screenshot(),
            "1/6  Briefing — Verified weekly KPIs, MAD anomalies, and exact GBP drivers",
            2200,
        )

        # 2. Click a KPI on Briefing to show the Show work drawer
        page.locator("#briefing-kpis .kpi-btn").first.click()
        page.wait_for_selector("#work-drawer:not(.hidden)", timeout=10000)
        page.wait_for_timeout(350)
        add_frame(
            frames,
            durations,
            page.screenshot(),
            "2/6  Show work — Every KPI opens its definition, SQL, and rows scanned",
            2100,
        )
        page.locator("#drawer-close").click()
        page.wait_for_timeout(200)

        # 3. Navigate to Ask and run a real question
        page.locator("a[data-nav='ask']").click()
        q = "What was net revenue in November 2011 vs the previous month?"
        page.fill("#ask-input", q)
        add_frame(
            frames,
            durations,
            page.screenshot(),
            "3/6  Ask — Plain-English question over governed metrics (pipeline/metrics.yml)",
            1600,
        )

        page.click("#ask-submit")
        page.wait_for_selector("#ask-result:not(.hidden)", timeout=45000)
        page.wait_for_timeout(600)
        add_frame(
            frames,
            durations,
            page.screenshot(),
            "4/6  Verified answer — DuckDB computes every figure; LLM never writes numbers",
            2300,
        )

        # 4. Click a highlighted number chip to focus its Show work panel
        chips = page.locator("#answer-text .num-chip")
        if chips.count() > 1:
            chips.nth(1).click()
            page.wait_for_timeout(350)
        add_frame(
            frames,
            durations,
            page.screenshot(),
            "5/6  Click any number — Inspect exact SQL, prior-period filter, and caveats",
            2300,
        )

        # 5. Open Clean and load sample messy retail CSV
        page.locator("a[data-nav='clean']").click()
        page.wait_for_selector("#clean-sample-btn", timeout=10000)
        page.click("#clean-sample-btn")
        page.wait_for_selector("#clean-workspace:not(.hidden)", timeout=20000)
        page.wait_for_timeout(450)
        add_frame(
            frames,
            durations,
            page.screenshot(),
            "6/6  Clean — Deterministic CSV issue detection, row preview, and formula guard",
            2700,
        )

        browser.close()

    # Quantize to adaptive 128-color palette so the 900px GIF stays crisp and well under 5 MB
    quantized = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for f in frames]
    OUT_GIF.parent.mkdir(parents=True, exist_ok=True)
    quantized[0].save(
        OUT_GIF,
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    size_mb = OUT_GIF.stat().st_size / (1024 * 1024)
    total_s = sum(durations) / 1000.0
    print(f"Saved {OUT_GIF} ({size_mb:.2f} MB, {len(frames)} frames, {total_s:.1f}s)")
    if size_mb >= 5.0:
        raise RuntimeError(f"GIF exceeded 5 MB ({size_mb:.2f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="Base URL, e.g. https://showwork.onrender.com")
    args = ap.parse_args()
    record(args.url)
