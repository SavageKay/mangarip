#!/usr/bin/env python3
"""
Selenium-based Manga Chapter Downloader
Downloads a chapter page as MHTML, extracts chapter images, and compiles a PDF.
Requires: selenium, webdriver-manager, Pillow
"""

import os
import re
import json
import time
import email
import base64
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse, urljoin

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

try:
    from webdriver_manager.chrome import ChromeDriverManager
    WDM_AVAILABLE = True
except ImportError:
    WDM_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _update(jobs, job_id, msg, progress_pct=None):
    print(msg)
    if job_id and jobs and job_id in jobs:
        jobs[job_id]["progress"] = msg
        if progress_pct is not None:
            jobs[job_id]["progress_pct"] = progress_pct


def _chrome_options():
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    return opts


def _save_as_mhtml(driver, dest_path):
    """Use Chrome DevTools Protocol to save the page as MHTML."""
    result = driver.execute_cdp_cmd("Page.captureSnapshot", {"format": "mhtml"})
    mhtml_data = result.get("data", "")
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(mhtml_data)
    return dest_path


# ---------------------------------------------------------------------------
# Image scoring / filtering
# ---------------------------------------------------------------------------

# Patterns that are almost certainly NOT chapter pages
_REJECT_URL_PATTERNS = re.compile(
    r"(avatar|profile|logo|icon|banner|ad|ads|advertisement|"
    r"social|share|button|nav|header|footer|widget|thumb(?:nail)?|"
    r"spinner|loading|placeholder|blank|pixel|1x1|tracking|"
    r"emoji|emoticon|gravatar|favicon)",
    re.IGNORECASE,
)

# Patterns that strongly suggest a chapter page image
_ACCEPT_URL_PATTERNS = re.compile(
    r"(chapter|chapters|chap|ch[-_]?\d|manga|manhwa|manhua|comic|"
    r"read|page|pages|p[-_]?\d|scan|raw|image|img|upload|cdn|content|"
    r"opti|optim|optimus|webp|jpg|jpeg|png)",
    re.IGNORECASE,
)

MIN_WIDTH  = 300   # px — reject tiny decorative images
MIN_HEIGHT = 400   # px — chapter pages are taller than wide
MIN_RATIO  = 0.5   # height / width — portrait only
MAX_RATIO  = 6.0   # reject absurdly tall images (likely banners)


def _score_image(src, natural_w, natural_h):
    """
    Return a float score for how likely this image is a chapter page.
    Positive = good, negative = likely UI/decoration.
    """
    score = 0.0

    # URL hints
    if _REJECT_URL_PATTERNS.search(src):
        score -= 50
    if _ACCEPT_URL_PATTERNS.search(src):
        score += 20

    # Size / ratio
    if natural_w and natural_h:
        if natural_w < MIN_WIDTH or natural_h < MIN_HEIGHT:
            score -= 30
        ratio = natural_h / natural_w if natural_w else 0
        if MIN_RATIO <= ratio <= MAX_RATIO:
            score += 15
        else:
            score -= 20
        # Bonus for typical manga widths (600-1200 px)
        if 500 <= natural_w <= 1400:
            score += 10

    return score


# ---------------------------------------------------------------------------
# MHTML extraction
# ---------------------------------------------------------------------------

def extract_images_from_mhtml(mhtml_path, extract_dir):
    """
    Parse an MHTML file and save all embedded images that look like
    chapter pages. Returns a sorted list of saved filenames.
    """
    with open(mhtml_path, "rb") as f:
        msg = email.message_from_bytes(f.read())

    saved = []
    idx = 0
    for part in msg.walk():
        ctype = part.get_content_type()
        if "image" not in ctype:
            continue
        loc = part.get("Content-Location", "") or part.get("Content-ID", "")
        data = part.get_payload(decode=True)
        if not data or len(data) < 2048:   # skip tiny images (<2KB)
            continue
        if _REJECT_URL_PATTERNS.search(loc):
            continue

        # Determine extension
        ext_map = {
            "image/jpeg": ".jpg", "image/jpg": ".jpg",
            "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif",
        }
        ext = ext_map.get(ctype.lower(), ".jpg")
        orig_name = loc.rstrip("/").split("/")[-1]
        if "." in orig_name:
            ext = Path(orig_name).suffix or ext

        fname = f"{idx:04d}{ext}"
        out_path = os.path.join(extract_dir, fname)
        with open(out_path, "wb") as f:
            f.write(data)

        # Basic dimension check via Pillow if available
        if PIL_AVAILABLE:
            try:
                with Image.open(out_path) as im:
                    w, h = im.size
                if w < MIN_WIDTH or h < MIN_HEIGHT:
                    os.remove(out_path)
                    continue
                ratio = h / w if w else 0
                if not (MIN_RATIO <= ratio <= MAX_RATIO):
                    os.remove(out_path)
                    continue
            except Exception:
                pass  # keep if we can't check

        saved.append(fname)
        idx += 1

    return sorted(saved)


# ---------------------------------------------------------------------------
# Main download function
# ---------------------------------------------------------------------------

def download_with_selenium(
    url,
    output_dir="downloaded_chapter",
    scroll_pause=1.5,
    max_scrolls=60,
    filter_string="opti",       # kept for API compat; scoring-based now
    job_id=None,
    jobs=None,
):
    """
    1. Open the chapter URL in headless Chrome.
    2. Scroll to trigger lazy-loading.
    3. Save the fully-rendered page as MHTML.
    4. Extract chapter images from the MHTML.
    5. Return paths so app.py can zip / compile.
    """

    def upd(msg, pct=None):
        _update(jobs, job_id, msg, pct)

    if not SELENIUM_AVAILABLE:
        upd("Error: selenium not installed. Run: pip install selenium")
        return
    if not WDM_AVAILABLE:
        upd("Error: webdriver-manager not installed. Run: pip install webdriver-manager")
        return

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    images_dir = output_path / "images"
    images_dir.mkdir(exist_ok=True)

    upd("Launching Chrome...", 5)

    driver = None
    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=_chrome_options(),
        )
        driver.set_page_load_timeout(60)
    except Exception as e:
        upd(f"Chrome init error: {e}")
        return

    try:
        upd(f"Loading page: {url}", 10)
        driver.get(url)

        # Wait for at least one image to appear
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "img"))
            )
        except Exception:
            pass
        time.sleep(2)

        # ── Scroll to trigger lazy loading ──────────────────────────────
        upd("Scrolling to load all images...", 15)
        last_height = driver.execute_script("return document.body.scrollHeight")
        unchanged = 0

        for scroll_num in range(max_scrolls):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(scroll_pause)
            # Nudge up slightly to help bidirectional lazy loaders
            driver.execute_script("window.scrollBy(0, -300);")
            time.sleep(0.2)

            new_height = driver.execute_script("return document.body.scrollHeight")
            pct = min(15 + int((scroll_num / max_scrolls) * 35), 50)
            upd(f"Scrolling… ({scroll_num + 1}/{max_scrolls})", pct)

            if new_height == last_height:
                unchanged += 1
                if unchanged >= 3:
                    upd("Page fully scrolled.", 50)
                    break
            else:
                unchanged = 0
            last_height = new_height

        # Scroll back to top then do a slow pass to ensure all images loaded
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
        total_h = driver.execute_script("return document.body.scrollHeight")
        steps = max(1, total_h // 800)
        for i in range(steps):
            driver.execute_script(f"window.scrollTo(0, {(i + 1) * 800});")
            time.sleep(0.15)

        # ── Inspect images via JS for scoring ──────────────────────────
        upd("Analysing images on page...", 55)
        img_data = driver.execute_script("""
            const imgs = Array.from(document.querySelectorAll('img'));
            return imgs.map(img => ({
                src: img.src || img.dataset.src || img.dataset.lazySrc || '',
                naturalW: img.naturalWidth,
                naturalH: img.naturalHeight,
                x: img.getBoundingClientRect().x,
                parentClass: (img.parentElement ? img.parentElement.className : ''),
                parentId: (img.parentElement ? img.parentElement.id : ''),
            }));
        """)

        # Score and filter
        chapter_srcs = []
        for item in img_data:
            src = item.get("src", "")
            if not src or src.startswith("data:"):
                continue
            w = item.get("naturalW") or 0
            h = item.get("naturalH") or 0
            score = _score_image(src, w, h)

            # Boost if parent element looks like a reader container
            parent_text = (item.get("parentClass", "") + " " + item.get("parentId", "")).lower()
            if any(k in parent_text for k in ("reader", "chapter", "page", "content", "comic", "manga")):
                score += 25

            if score > 0:
                chapter_srcs.append((score, src))

        # Deduplicate preserving order, highest score wins per URL
        seen = {}
        for score, src in chapter_srcs:
            base = src.split("?")[0]
            if base not in seen or score > seen[base][0]:
                seen[base] = (score, src)

        ordered = [src for _, src in sorted(seen.values(), reverse=True)]

        # If we have very few candidates, relax the filter
        if len(ordered) < 3:
            ordered = [item["src"] for item in img_data
                       if item.get("src") and not item["src"].startswith("data:")]

        upd(f"Found {len(ordered)} candidate chapter images.", 58)

        # ── Save page as MHTML ───────────────────────────────────────────
        upd("Saving page as MHTML...", 60)
        mhtml_path = str(output_path / "page.mhtml")
        _save_as_mhtml(driver, mhtml_path)
        upd("MHTML saved.", 65)

    except Exception as e:
        upd(f"Fatal error during page capture: {e}")
        return
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    # ── Extract images from MHTML ────────────────────────────────────────
    upd("Extracting chapter images from MHTML...", 70)
    extracted = extract_images_from_mhtml(mhtml_path, str(images_dir))
    upd(f"Extracted {len(extracted)} images from MHTML.", 85)

    # If MHTML extraction gave too few images, fall back to scored URL list
    if len(extracted) < 3 and ordered:
        upd("MHTML extraction incomplete — downloading images directly...", 87)
        import requests
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": url,
        }
        n = len(str(len(ordered)))
        downloaded = []
        for i, src in enumerate(ordered):
            try:
                r = requests.get(src, headers=headers, timeout=20)
                r.raise_for_status()
                ct = r.headers.get("content-type", "")
                ext = (
                    ".jpg" if "jpeg" in ct else
                    ".png" if "png" in ct else
                    ".webp" if "webp" in ct else ".jpg"
                )
                fname = f"{i:0{n}d}{ext}"
                fpath = images_dir / fname
                with open(fpath, "wb") as f:
                    f.write(r.content)
                downloaded.append(fname)
                time.sleep(0.15)
            except Exception as ex:
                print(f"  Skip {src}: {ex}")
        extracted = downloaded
        upd(f"Downloaded {len(extracted)} images directly.", 92)

    upd(f"Done — {len(extracted)} chapter pages ready.", 95)
    return {
        "mhtml_path": mhtml_path,
        "images_dir": str(images_dir),
        "image_files": extracted,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("MangaRip — Selenium Chapter Downloader")
    print("=" * 60)

    if not SELENIUM_AVAILABLE:
        print("selenium not installed. Run: pip install selenium")
        return
    if not WDM_AVAILABLE:
        print("webdriver-manager not installed. Run: pip install webdriver-manager")
        return

    url = input("\nChapter URL: ").strip()
    if not url:
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    out = input("Output directory [downloaded_chapter]: ").strip() or "downloaded_chapter"
    download_with_selenium(url, out)


if __name__ == "__main__":
    main()
