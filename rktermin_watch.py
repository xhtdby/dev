#!/usr/bin/env python3
"""Check German Foreign Office RK-Termin London category 4019 for bookable days.

Primary fetch path uses Jina Reader as an HTML-to-text proxy because service2.diplo.de
currently times out requests from GitHub-hosted runners. If Jina cannot reach the page,
the script fails closed (no false availability alert).
"""

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
DIPLO_HOST = "service2.diplo.de"
BASE = f"https://{DIPLO_HOST}/rktermin/extern/appointment_showMonth.do"
JINA_PREFIX = "https://r.jina.ai/https://"
TIMEOUT_SECONDS = 20
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


def jina_url(target_url: str) -> str:
    parsed = urllib.parse.urlsplit(target_url)
    if parsed.scheme != "https" or parsed.netloc != DIPLO_HOST:
        raise ValueError("unexpected target URL")
    return JINA_PREFIX + parsed.netloc + parsed.path + ("?" + parsed.query if parsed.query else "")


def fetch_via_jina(target_url: str) -> str:
    proxy_url = jina_url(target_url)
    req = urllib.request.Request(
        proxy_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/plain,text/markdown;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        if r.status != 200:
            raise RuntimeError(f"Jina HTTP {r.status}")
        return r.read().decode("utf-8", errors="replace")


def booking_links(body: str, page_url: str):
    decoded = html.unescape(body)
    candidates = []

    # Raw HTML hrefs, if the proxy returns HTML.
    candidates.extend(
        re.findall(r'href=["\']([^"\']*appointment_showDay\.do[^"\']*)["\']', decoded, flags=re.I)
    )

    # Markdown/plain-text URLs, which is Jina Reader's normal output.
    candidates.extend(
        re.findall(r'https?://[^\s\)\]>"\']*appointment_showDay\.do[^\s\)\]>"\']*', decoded, flags=re.I)
    )

    out = []
    for href in candidates:
        full = urllib.parse.urljoin(page_url, href)
        parsed = urllib.parse.urlparse(full)
        if parsed.netloc != DIPLO_HOST:
            continue
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
    body = fetch_via_jina(url)
    links = booking_links(body, url)
    lowered = body.lower()
    no_slot = any(p.lower() in lowered for p in NO_SLOT_PHRASES)
    captcha = "captcha" in lowered
    snippet = ""
    if not links and not no_slot:
        snippet = re.sub(r"\s+", " ", html.unescape(body)).strip()[:700]
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

    # Only accept a no-slot result when every page contained a known no-slot phrase.
    if results and all(r[2] for r in results):
        print("AVAILABLE=0")
        return 1

    print("CHECK_FAILED=page layout/state was not recognized; refusing to infer availability", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
