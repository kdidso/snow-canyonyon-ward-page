#!/usr/bin/env python3
import json
import re
import sys
import os
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.churchofjesuschrist.org"
MANUAL_PATH = "/study/manual/come-follow-me-for-home-and-church-old-testament-2026/{week:02d}?lang=eng"
OUT_JSON = "data/come_follow_me_this_week.json"


def iso_week_number(d: date) -> int:
    """
    ISO week can be 1..53. Your manual appears to be 1..52.
    Clamp to 52 so week 53 doesn't break the URL.
    """
    wk = d.isocalendar().week
    return min(int(wk), 52)


def absolute_url(u: str) -> str:
    if not u:
        return ""
    return urljoin(BASE, u)


def get_text_or_empty(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _largest_from_srcset(srcset: str) -> str:
    """
    srcset like: 'url1 60w, url2 100w, url3 640w'
    Return the URL with the largest width.
    """
    if not srcset:
        return ""
    candidates = []
    for part in srcset.split(","):
        part = part.strip()
        m = re.match(r"(\S+)\s+(\d+)w", part)
        if m:
            candidates.append((int(m.group(2)), m.group(1)))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def pick_best_image_from_tag(img_tag) -> str:
    """
    Given an <img> tag, prefer the largest srcset candidate, otherwise src.
    """
    if not img_tag:
        return ""
    srcset = img_tag.get("srcset", "")
    best = _largest_from_srcset(srcset)
    if best:
        return absolute_url(best)
    src = img_tag.get("src", "")
    if src:
        return absolute_url(src)
    return ""


def pick_first_image(soup: BeautifulSoup) -> str:
    """
    Prefer the header hero image (img#img1).
    If missing, fall back to first <figure> image, then any <img>.
    Always prefer the LARGEST srcset candidate when available.
    """
    # 1) Desired header image
    img = soup.select_one("img#img1")
    best = pick_best_image_from_tag(img)
    if best:
        return best

    # 2) Fallback: first figure image
    fig_img = soup.select_one("figure img")
    best = pick_best_image_from_tag(fig_img)
    if best:
        return best

    # 3) Fallback: any image
    any_img = soup.find("img")
    best = pick_best_image_from_tag(any_img)
    if best:
        return best

    return ""


def scrape_week(week: int) -> dict:
    url = BASE + MANUAL_PATH.format(week=week)

    r = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; SnowCanyonWardBot/1.0; +https://github.com/kdidso)"
        },
    )
    r.raise_for_status()

    # Force correct decoding so curly quotes / dashes survive
    r.encoding = "utf-8"

    soup = BeautifulSoup(r.text, "html.parser")

    # Small heading is typically p.title-number
    small_heading_el = soup.select_one("p.title-number")
    # Big heading is typically the first h1
    big_heading_el = soup.select_one("h1")

    small_heading = get_text_or_empty(small_heading_el)
    big_heading = get_text_or_empty(big_heading_el)

    image_url = pick_first_image(soup)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "week_number": week,
        "source_url": url,
        "image_url": image_url,
        "small_heading": small_heading,
        "big_heading": big_heading,
    }


def main():
    today = date.today()
    week = iso_week_number(today)

    try:
        payload = scrape_week(week)
    except Exception as e:
        print(f"ERROR scraping week {week}: {e}", file=sys.stderr)
        raise

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote {OUT_JSON} for week {week}: {payload.get('big_heading','')}")


if __name__ == "__main__":
    main()
