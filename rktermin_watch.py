#!/usr/bin/env python3
"""Check German Foreign Office RK-Termin London category 4019 for bookable days."""

from __future__ import annotations

import concurrent.futures
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
TIMEOUT_SECONDS = 12
MONTHS_TO_CHECK = 8

NO_SLOT_PHRASES = (
    "Unfortunately, there are no appointments available at this time",
    "No appointments available for this period",
    "In diesem Zeitraum sind keine Termine frei",
    "Leider sind aktuell keine Termine verfügbar",
)


def month_starts(count: int = MONTHS_TO_CHECK):
    today = dt.date.today()
    year, month = today.year, today.month
    for offset in range(count):
        m = month + offset
        y = year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        yield dt.date(y, m, 1)


def make_month_url(d: dt.date) -> str:
    params = {
        "locationCode": LOCATION,
        "realmId": REALM,
        "categoryId": CATEGORY,
        "dateStr": d.strftime("%d.%m.%Y"),
    }
    return BASE + "?" + urllib.parse.urlencode(params)


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9,de;q=0.7",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read().decode("utf-8", errors="replace")


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


def inspect(url: str):
    body = fetch(url)
    links = booking_links(body, url)
    lowered = body.lower()
    no_slot = any(p.lower() in lowered for p in NO_SLOT_PHRASES)
    captcha = "captcha" in lowered
    snippet = ""
    if not links and not no_slot:
        text = re.sub(r"<[^>]+>", " ", body)
        snippet = re.sub(r"\s+", " ", html.unescape(text)).strip()[:500]
    return url, links, no_slot, captcha, len(body), snippet


def main() -> int:
    urls = [make_month_url(d) for d in month_starts()]
    results = []
    failures = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(urls)) as pool:
        future_to_url = {pool.submit(inspect, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append((url, repr(exc)))

    for url, err in sorted(failures):
        print(f"FETCH_ERROR={url} :: {err}", file=sys.stderr)

    if failures:
        print(f"CHECK_FAILED={len(failures)}/{len(urls)} month pages could not be fetched", file=sys.stderr)
        return 2

    all_links = []
    unknown = []
    for url, links, no_slot, captcha, size, snippet in sorted(results):
        print(f"checked={url} day_links={len(links)} no_slot_phrase={no_slot} captcha={captcha} bytes={size}")
        for link in links:
            if link not in all_links:
                all_links.append(link)
        if snippet:
            unknown.append((url, snippet))

    if unknown:
        print("UNKNOWN_PAGES_BEGIN")
        for url, snippet in unknown:
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
