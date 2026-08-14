#!/usr/bin/env python3
"""Check German Foreign Office RK-Termin London category 4019 for bookable days."""

from __future__ import annotations

import datetime as dt
import html
import re
import sys
import urllib.parse
import urllib.request

LOCATION = "lond"
REALM = "1614"
CATEGORY = "4019"
BASE = "https://service2.diplo.de/rktermin/extern/appointment_showMonth.do"

NO_SLOT_PHRASES = (
    "Unfortunately, there are no appointments available at this time",
    "No appointments available for this period",
    "In diesem Zeitraum sind keine Termine frei",
    "Leider sind aktuell keine Termine verfügbar",
)


def month_starts(count: int = 8):
    today = dt.date.today()
    year, month = today.year, today.month
    for offset in range(count):
        m = month + offset
        y = year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        yield dt.date(y, m, 1)


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9,de;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} for {url}")
        return r.read().decode("utf-8", errors="replace")


def make_month_url(d: dt.date) -> str:
    params = {
        "locationCode": LOCATION,
        "realmId": REALM,
        "categoryId": CATEGORY,
        "dateStr": d.strftime("%d.%m.%Y"),
    }
    return BASE + "?" + urllib.parse.urlencode(params)


def booking_links(body: str, page_url: str):
    decoded = html.unescape(body)
    hrefs = re.findall(r'href=["\']([^"\']*appointment_showDay\.do[^"\']*)["\']', decoded, flags=re.I)
    out = []
    for href in hrefs:
        full = urllib.parse.urljoin(page_url, href)
        parsed = urllib.parse.urlparse(full)
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("locationCode", [LOCATION])[0] != LOCATION:
            continue
        if qs.get("realmId", [REALM])[0] != REALM:
            continue
        if qs.get("categoryId", [CATEGORY])[0] != CATEGORY:
            continue
        if full not in out:
            out.append(full)
    return out


def main() -> int:
    all_links = []
    checked = []
    unknown_pages = []

    for month in month_starts():
        url = make_month_url(month)
        try:
            body = fetch(url)
        except Exception as exc:
            print(f"ERROR fetching {url}: {exc}", file=sys.stderr)
            return 2

        links = booking_links(body, url)
        no_slot = any(p.lower() in body.lower() for p in NO_SLOT_PHRASES)
        captcha = "captcha" in body.lower()
        checked.append((url, len(links), no_slot, captcha, len(body)))
        all_links.extend(link for link in links if link not in all_links)

        if not links and not no_slot:
            # Unknown layout/state: log it, but do not alert. This avoids false positives.
            text = re.sub(r"<[^>]+>", " ", body)
            text = re.sub(r"\s+", " ", html.unescape(text)).strip()
            unknown_pages.append((url, text[:500]))

    for url, link_count, no_slot, captcha, size in checked:
        print(f"checked={url} day_links={link_count} no_slot_phrase={no_slot} captcha={captcha} bytes={size}")

    if unknown_pages:
        print("UNKNOWN_PAGES_BEGIN")
        for url, snippet in unknown_pages:
            print(url)
            print(snippet)
        print("UNKNOWN_PAGES_END")

    if all_links:
        print("AVAILABLE=1")
        for link in all_links:
            print(f"BOOKING_LINK={link}")
        return 0

    print("AVAILABLE=0")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
