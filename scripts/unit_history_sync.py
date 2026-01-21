#!/usr/bin/env python3
import os
import re
import json
import time
import zipfile
import base64
import pathlib
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE = "https://unithistory.churchofjesuschrist.org"
START_URL = "https://unithistory.churchofjesuschrist.org/"

# Your confirmed story-card class (two classes on same element)
STORY_CARD_SELECTOR = ".sc-1m4vew7-0.bYMucc"

# Env-configurable outputs
OUT_DIR = os.getenv("OUT_DIR", "unit-history/events").strip() or "unit-history/events"
ZIP_NAME = os.getenv("ZIP_NAME", "unit-history/unit_history_export.zip").strip() or "unit-history/unit_history_export.zip"
MANIFEST_PATH = os.getenv("MANIFEST_PATH", "unit-history/manifest.json").strip() or "unit-history/manifest.json"

HEADLESS = os.getenv("HEADLESS", "1").strip().lower() not in ("0", "false", "no", "")
SKIP_EXISTING_FOLDERS = os.getenv("SKIP_EXISTING_FOLDERS", "1").strip().lower() not in ("0", "false", "no", "")

# Auth env
STORAGE_STATE_B64 = os.getenv("UNIT_HISTORY_STORAGE_STATE_B64", "").strip()
LDS_USERNAME = os.getenv("LDS_USERNAME", "").strip()
LDS_PASSWORD = os.getenv("LDS_PASSWORD", "").strip()

# ---------------------------
# Utilities
# ---------------------------
def safe_name(name: str, max_len: int = 90) -> str:
    name = (name or "").strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)  # Windows-safe
    name = name.strip(" .")
    if not name:
        name = "Untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name

def save_debug(page, tag="debug"):
    ts = time.strftime("%Y%m%d-%H%M%S")
    shot = f"{tag}_{ts}.png"
    html = f"{tag}_{ts}.html"
    try:
        page.screenshot(path=shot, full_page=True)
    except Exception:
        pass
    try:
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass
    print(f"🧪 Saved debug screenshot: {shot}")
    print(f"🧪 Saved debug HTML:       {html}")

def absolutize(href: str) -> str:
    if not href:
        return ""
    return urljoin(BASE, href)

def pick_largest_from_srcset(srcset: str) -> str:
    """
    srcset: "url1 480w, url2 800w, url3 1200w"
    return url with the largest width. If no widths, return first URL.
    """
    if not srcset:
        return ""
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    candidates = []
    for p in parts:
        m = re.match(r"(\S+)\s+(\d+)w", p)
        if m:
            candidates.append((int(m.group(2)), m.group(1)))
        else:
            tok = p.split()
            if tok:
                candidates.append((0, tok[0]))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]

def normalize_img_url(u: str) -> str:
    if not u:
        return ""
    u = str(u).strip().strip(' "\'')
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("/"):
        u = absolutize(u)
    return u

def scroll_to_load(page, max_scrolls=18, pause_ms=350):
    for _ in range(max_scrolls):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(pause_ms)
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(250)

def is_login_page(url: str) -> bool:
    u = (url or "").lower()
    return (
        ("signin" in u) or ("login" in u) or ("okta" in u) or
        ("auth" in u and "churchofjesuschrist.org" in u)
    )

def file_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"]:
        return ext
    return ".jpg"

def strip_downscaling_params(url: str) -> str:
    """
    Some CDNs/thumb services add params like w/h/width/height/fit/quality etc.
    Removing them can yield a larger image.
    """
    try:
        pu = urlparse(url)
        qs = parse_qs(pu.query, keep_blank_values=True)

        remove_keys = {
            "w", "h", "width", "height",
            "fit", "crop", "rect",
            "q", "quality",
            "auto", "format",
            "dpr"
        }
        changed = False
        for k in list(qs.keys()):
            if k.lower() in remove_keys:
                qs.pop(k, None)
                changed = True

        if not changed:
            return url

        new_query = urlencode(qs, doseq=True)
        return urlunparse((pu.scheme, pu.netloc, pu.path, pu.params, new_query, pu.fragment))
    except Exception:
        return url

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def zip_folder(root: pathlib.Path, zip_name: str):
    ensure_dir(pathlib.Path(zip_name).parent)
    with zipfile.ZipFile(zip_name, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in root.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(root))
    print(f"📦 Wrote zip: {zip_name}")

# ---------------------------
# Auth helpers
# ---------------------------
def write_storage_state_from_b64(path: str, b64: str):
    data = base64.b64decode(b64.encode("utf-8"))
    with open(path, "wb") as f:
        f.write(data)
    print(f"🔐 Wrote storage state to {path} from UNIT_HISTORY_STORAGE_STATE_B64")

def attempt_headless_login(page):
    """
    Best-effort headless login.
    This may fail if MFA is required or the login flow changes.
    """
    if not LDS_USERNAME or not LDS_PASSWORD:
        raise RuntimeError("No storage_state provided and LDS_USERNAME/LDS_PASSWORD not set.")

    print("🔐 Attempting headless login with LDS_USERNAME/LDS_PASSWORD...")

    page.goto(START_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1500)

    # If we aren't redirected to a login page, we might already be authenticated
    if not is_login_page(page.url):
        print("✅ Not on a login page; likely already authenticated.")
        return

    # Generic form-fill attempts (works only if the page uses standard inputs)
    # We intentionally try multiple common selectors.
    filled = False
    selectors_user = [
        "input[type='email']",
        "input[name='username']",
        "input#username",
        "input[id*='user']",
        "input[autocomplete='username']",
    ]
    selectors_pass = [
        "input[type='password']",
        "input[name='password']",
        "input#password",
        "input[autocomplete='current-password']",
    ]
    for su in selectors_user:
        try:
            if page.locator(su).count() > 0:
                page.locator(su).first.fill(LDS_USERNAME, timeout=8000)
                filled = True
                break
        except Exception:
            continue

    if not filled:
        save_debug(page, tag="login_no_username_field")
        raise RuntimeError("Could not find username/email input on login page.")

    filled_p = False
    for sp in selectors_pass:
        try:
            if page.locator(sp).count() > 0:
                page.locator(sp).first.fill(LDS_PASSWORD, timeout=8000)
                filled_p = True
                break
        except Exception:
            continue

    if not filled_p:
        save_debug(page, tag="login_no_password_field")
        raise RuntimeError("Could not find password input on login page.")

    # Submit
    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Sign in')",
        "button:has-text('Sign In')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
    ]
    submitted = False
    for ss in submit_selectors:
        try:
            if page.locator(ss).count() > 0:
                page.locator(ss).first.click(timeout=8000)
                submitted = True
                break
        except Exception:
            continue

    if not submitted:
        # Try pressing Enter in password field
        try:
            page.keyboard.press("Enter")
            submitted = True
        except Exception:
            pass

    page.wait_for_timeout(3000)

    # If still on login page, it's likely MFA or a different flow.
    if is_login_page(page.url):
        save_debug(page, tag="login_still_on_login_page")
        raise RuntimeError(
            "Login still appears to be required (possibly MFA/2FA). "
            "Use UNIT_HISTORY_STORAGE_STATE_B64 instead."
        )

    print("✅ Headless login appears successful.")

# ---------------------------
# Grid interactions
# ---------------------------
def open_story_grid(page):
    print(f"➡️  Opening: {START_URL}")
    page.goto(START_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1200)

    if is_login_page(page.url):
        save_debug(page, tag="redirected_to_login")
        raise RuntimeError("Redirected to login. Authentication was not valid.")

    print(f"⏳ Waiting for story cards: {STORY_CARD_SELECTOR}")
    try:
        page.wait_for_selector(STORY_CARD_SELECTOR, timeout=60_000)
    except PWTimeoutError:
        save_debug(page, tag="story_cards_not_found")
        raise RuntimeError(f"Could not find story cards using selector: {STORY_CARD_SELECTOR}")

    page.wait_for_timeout(800)

def get_story_card_count(page) -> int:
    return page.locator(STORY_CARD_SELECTOR).count()

def get_card_title(card_locator) -> str:
    for sel in ["h2", "h3", "[role='heading']"]:
        try:
            t = card_locator.locator(sel).first.inner_text(timeout=500).strip()
            if t:
                return t
        except Exception:
            pass

    for sel in ["strong", "b"]:
        try:
            t = card_locator.locator(sel).first.inner_text(timeout=500).strip()
            if t:
                return t
        except Exception:
            pass

    try:
        a = card_locator.get_attribute("aria-label") or ""
        if a.strip():
            return a.strip()
    except Exception:
        pass

    try:
        txt = card_locator.inner_text(timeout=1000).strip()
        txt = re.sub(r"\s+", " ", txt)
        if txt:
            return txt[:80]
    except Exception:
        pass

    return "Untitled"

# ---------------------------
# Story page: extract images + metadata
# ---------------------------
def extract_image_urls_from_dom(page) -> list[str]:
    raw = page.evaluate("""
      () => {
        const urls = new Set();

        for (const img of Array.from(document.querySelectorAll('img'))) {
          const src = img.getAttribute('src') || '';
          const dataSrc = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
          const srcset = img.getAttribute('srcset') || '';
          if (src) urls.add(src);
          if (dataSrc) urls.add(dataSrc);
          if (srcset) urls.add(srcset);
        }

        for (const s of Array.from(document.querySelectorAll('picture source'))) {
          const srcset = s.getAttribute('srcset') || '';
          if (srcset) urls.add(srcset);
        }

        return Array.from(urls);
      }
    """)

    out = set()
    for item in raw:
        if not item:
            continue
        item = str(item).strip()

        if "," in item and ("w" in item or "x" in item):
            best = pick_largest_from_srcset(item)
            best = normalize_img_url(best)
            if best:
                out.add(best)
        else:
            u = normalize_img_url(item)
            if u:
                out.add(u)

    cleaned = []
    for u in out:
        low = u.lower()
        if low.startswith("data:") or low.startswith("blob:"):
            continue
        cleaned.append(u)

    cleaned.sort()
    return cleaned

def guess_story_title_date(page) -> tuple[str, str]:
    title = "Untitled"
    date_str = ""

    try:
        title = page.locator("h1").first.inner_text(timeout=8000).strip() or "Untitled"
    except Exception:
        pass

    try:
        date_candidate = page.locator(
            "text=/\\b(Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\\b/i"
        ).first
        date_str = date_candidate.inner_text(timeout=3000).strip()
        if len(date_str) > 40:
            date_str = date_str[:40].strip()
    except Exception:
        date_str = ""

    return title, date_str

def download_file_via_context(context, url: str, dest_path: pathlib.Path) -> bool:
    try:
        resp = context.request.get(url, timeout=120_000)
        if not resp.ok:
            return False
        dest_path.write_bytes(resp.body())
        return True
    except Exception:
        return False

def _try_close_lightbox(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass

    for sel in [
        "button[aria-label*='Close']",
        "button[title*='Close']",
        "[role='button'][aria-label*='Close']",
        "button:has-text('Close')",
        "button:has-text('close')",
        "button:has-text('×')",
        "button:has-text('X')",
        "svg[aria-label*='Close']",
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=800)
                page.wait_for_timeout(250)
                break
        except Exception:
            pass

def collect_fullsize_urls_via_lightbox(page) -> list[str]:
    thumb_selectors = [
        "main img",
        "article img",
        "img",
    ]

    lightbox_img_selectors = [
        "[role='dialog'] img",
        ".modal img",
        ".lightbox img",
        "div[aria-modal='true'] img",
        "img[style*='max-width']",
        "img[style*='maxHeight']",
    ]

    thumbs = None
    for sel in thumb_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            thumbs = loc
            break

    if thumbs is None or thumbs.count() == 0:
        return []

    candidate_indices = []
    count = thumbs.count()
    for i in range(count):
        try:
            el = thumbs.nth(i)
            box = el.bounding_box()
            if not box:
                continue
            if box["width"] < 120 or box["height"] < 90:
                continue
            src = (el.get_attribute("src") or "") + " " + (el.get_attribute("srcset") or "")
            src = src.lower()
            if "unithistory" in src or "blob.core.windows.net" in src or "image" in src:
                candidate_indices.append(i)
            else:
                candidate_indices.append(i)
        except Exception:
            continue

    seen = set()
    ordered = []
    for i in candidate_indices:
        if i not in seen:
            seen.add(i)
            ordered.append(i)

    full_urls = []
    for idx in ordered:
        try:
            thumb = thumbs.nth(idx)
            thumb.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(150)

            try:
                thumb.click(timeout=5000)
            except Exception:
                try:
                    thumb.locator("xpath=..").click(timeout=5000)
                except Exception:
                    continue

            page.wait_for_timeout(350)

            best = ""
            for sel in lightbox_img_selectors:
                try:
                    img = page.locator(sel).first
                    if img.count() == 0:
                        continue
                    srcset = img.get_attribute("srcset") or ""
                    if srcset.strip():
                        best = pick_largest_from_srcset(srcset)
                        best = normalize_img_url(best)
                    if not best:
                        src = img.get_attribute("src") or ""
                        best = normalize_img_url(src)
                    if best:
                        break
                except Exception:
                    continue

            if best:
                full_urls.append(best)

            _try_close_lightbox(page)
            page.wait_for_timeout(150)

        except Exception:
            _try_close_lightbox(page)
            continue

    upgraded = [strip_downscaling_params(u) for u in full_urls]

    seen = set()
    out = []
    for u in upgraded:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def download_current_story(page, context, out_root: pathlib.Path) -> dict:
    try:
        page.wait_for_selector("h1", timeout=45_000)
    except PWTimeoutError:
        save_debug(page, tag="story_no_h1")

    scroll_to_load(page, max_scrolls=18, pause_ms=350)

    title, date_str = guess_story_title_date(page)
    folder_base = safe_name(title)
    if date_str:
        folder_base = safe_name(f"{folder_base} - {date_str}")

    story_folder = out_root / folder_base

    # Skip rule: if already exists, skip entirely
    if SKIP_EXISTING_FOLDERS and story_folder.exists():
        print(f"⏭️  Skipping (folder already exists): {story_folder}")
        return {
            "final_url": page.url,
            "title": title,
            "date_text": date_str,
            "skipped_existing_folder": True,
            "folder": str(story_folder),
            "image_count_found": 0,
            "images": [],
        }

    ensure_dir(story_folder)

    # 1) Best attempt: fullsize via lightbox
    image_urls = collect_fullsize_urls_via_lightbox(page)

    # 2) Fallback: DOM scrape
    if not image_urls:
        dom_urls = extract_image_urls_from_dom(page)
        dom_urls = [strip_downscaling_params(u) for u in dom_urls]
        seen = set()
        image_urls = []
        for u in dom_urls:
            if u and u not in seen:
                seen.add(u)
                image_urls.append(u)

    meta = {
        "final_url": page.url,
        "title": title,
        "date_text": date_str,
        "folder": str(story_folder),
        "image_count_found": len(image_urls),
        "images": [],
    }

    print(f"🗂️  Story: {title} ({len(image_urls)} images)")

    for idx, img_url in enumerate(image_urls, start=1):
        ext = file_ext_from_url(img_url)
        fname = f"{idx:03d}{ext}"
        dest = story_folder / fname

        ok = download_file_via_context(context, img_url, dest)
        if not ok:
            page.wait_for_timeout(600)
            ok = download_file_via_context(context, img_url, dest)

        meta["images"].append({
            "index": idx,
            "url": img_url,
            "file": str(dest),
            "downloaded": bool(ok),
        })

        if ok:
            print(f"  ✅ {fname}")
        else:
            print(f"  ⚠️  FAILED {fname}  ({img_url})")

    with open(story_folder / "story.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return meta

# ---------------------------
# Main
# ---------------------------
def main():
    out_root = pathlib.Path(OUT_DIR)
    ensure_dir(out_root)

    # Temp auth state path (in repo workspace)
    storage_state_path = "storage_state.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)

        # Build context with either storage state or login
        if STORAGE_STATE_B64:
            write_storage_state_from_b64(storage_state_path, STORAGE_STATE_B64)
            context = browser.new_context(storage_state=storage_state_path)
            page = context.new_page()
        else:
            context = browser.new_context()
            page = context.new_page()
            attempt_headless_login(page)
            # Save state for the rest of this run
            context.storage_state(path=storage_state_path)
            print("🔐 Saved storage_state.json for this workflow run.")
            # Rebuild context using the stored state (cleaner + consistent)
            context.close()
            context = browser.new_context(storage_state=storage_state_path)
            page = context.new_page()

        # Open grid
        open_story_grid(page)
        total = get_story_card_count(page)
        print(f"✅ Found {total} story cards using {STORY_CARD_SELECTOR}")

        if total <= 0:
            save_debug(page, tag="zero_cards")
            raise RuntimeError("Found zero cards; selector is wrong or page didn't load.")

        all_meta = []
        processed = 0
        skipped = 0

        for i in range(total):
            print(f"\n=== [{i+1}/{total}] ===")

            open_story_grid(page)

            cards = page.locator(STORY_CARD_SELECTOR)
            if i >= cards.count():
                print("⚠️  Card count changed after reload; stopping.")
                break

            card = cards.nth(i)
            card_title = get_card_title(card)
            print(f"📌 Clicking card: {card_title}")

            clicked = False
            try:
                card.click(timeout=20_000)
                clicked = True
            except Exception:
                pass

            if not clicked:
                try:
                    card.locator("a").first.click(timeout=20_000)
                    clicked = True
                except Exception:
                    pass

            if not clicked:
                save_debug(page, tag=f"cannot_click_card_{i+1}")
                print("❌ Could not click card; skipping.")
                continue

            if is_login_page(page.url):
                save_debug(page, tag=f"login_bounce_story_{i+1}")
                raise RuntimeError("Bounced to login mid-run. Auth is not valid.")

            meta = download_current_story(page, context, out_root)
            meta["grid_title_guess"] = card_title
            all_meta.append(meta)

            if meta.get("skipped_existing_folder"):
                skipped += 1
            else:
                processed += 1

        manifest = {
            "exported_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "base": BASE,
            "start_url": START_URL,
            "headless": bool(HEADLESS),
            "skip_existing_folders": bool(SKIP_EXISTING_FOLDERS),
            "story_count_found_on_grid": total,
            "story_count_downloaded_new": processed,
            "story_count_skipped_existing": skipped,
            "stories": all_meta,
        }

        ensure_dir(pathlib.Path(MANIFEST_PATH).parent)
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # Optional zip (handy for manual download too)
        zip_folder(out_root, ZIP_NAME)

        context.close()
        browser.close()

    print("\n✅ Done.")

if __name__ == "__main__":
    main()
