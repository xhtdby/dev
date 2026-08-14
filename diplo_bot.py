#!/usr/bin/env python3
"""RK-Termin watcher + review-gated booking assistant.

Design:
- Terminwatch-style Diplo month/day discovery.
- Playwright persistent browser session with optional proxy.
- Generic/configurable form autofill.
- Telegram review card with a single BOOK / SKIP decision.
- Never clicks the final submit button before explicit BOOK approval.

This bot does not solve CAPTCHAs or OTP challenges. If one appears, it pauses
and notifies the reviewer instead of attempting to bypass it.
"""

from __future__ import annotations

import asyncio
import calendar
import dataclasses
import datetime as dt
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from playwright.async_api import BrowserContext, Error as PlaywrightError, Locator, Page, async_playwright


DIPLO_ORIGIN = "https://service2.diplo.de"
MONTH_PATH = "/rktermin/extern/appointment_showMonth.do"
DAY_PATH = "/rktermin/extern/appointment_showDay.do"

NO_SLOT_PHRASES = (
    "no appointments available for this period",
    "unfortunately, there are no appointments available at this time",
    "there are no appointments available at this time",
    "in diesem zeitraum sind keine termine frei",
    "leider sind aktuell keine termine verfügbar",
    "all appointments for this date are already taken",
    "on this date bookings are not possible",
    "for this date no appointments are offered",
    "an diesem tag sind alle termine belegt",
    "an diesem tag können keine termine gebucht werden",
    "an diesem tag werden keine termine angeboten",
)

CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "turnstile",
)

OTP_MARKERS = (
    "one-time password",
    "one time password",
    "verification code",
    "security code",
    "tan code",
    "otp",
)

DEFAULT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "firstname", "given name", "forename", "vorname"),
    "last_name": ("last name", "lastname", "surname", "family name", "nachname"),
    "email": ("email", "e-mail", "mail address", "email address"),
    "email_repeat": ("repeat email", "confirm email", "email confirmation", "e-mail wiederholen"),
    "phone": ("phone", "telephone", "mobile", "telefon", "handy"),
    "passport_number": ("passport", "passport number", "pass number", "reisepass", "passnummer"),
    "date_of_birth": ("date of birth", "birth date", "birthday", "geburtsdatum"),
    "place_of_birth": ("place of birth", "birth place", "geburtsort"),
    "nationality": ("nationality", "citizenship", "staatsangehörigkeit", "nationalität"),
    "street": ("street", "address", "straße", "strasse"),
    "city": ("city", "town", "ort", "stadt"),
    "postal_code": ("postal code", "postcode", "zip", "postleitzahl", "plz"),
    "country": ("country", "land"),
}

SUBMIT_WORDS = (
    "book",
    "book appointment",
    "confirm",
    "submit",
    "send",
    "continue",
    "appointment",
    "buchen",
    "termin buchen",
    "bestätigen",
    "absenden",
    "weiter",
)

BOOKING_URL_HINTS = (
    "newappointment",
    "appointment_new",
    "appointment_register",
    "appointment_showform",
    "appointment_form",
    "appointment_edit",
)


@dataclasses.dataclass
class Target:
    location_code: str
    realm_id: str
    category_id: str
    months_ahead: int = 8
    poll_seconds: float = 30.0
    start_date: dt.date | None = None
    end_date: dt.date | None = None


@dataclasses.dataclass
class TelegramConfig:
    bot_token: str | None
    chat_id: str | None
    allowed_user_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclasses.dataclass
class BotConfig:
    target: Target
    profile: dict[str, Any]
    selectors: dict[str, str]
    checkbox_selectors: list[str]
    telegram: TelegramConfig
    user_data_dir: str = ".diplo-profile"
    headless: bool = True
    navigation_timeout_ms: int = 45_000
    proxy: dict[str, str] | None = None
    exit_after_booking: bool = True
    screenshot_dir: str = "screenshots"


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unsupported date format: {value!r}")


def load_config(path: str) -> BotConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    target_raw = raw.get("target", {})
    target = Target(
        location_code=str(target_raw["location_code"]),
        realm_id=str(target_raw["realm_id"]),
        category_id=str(target_raw["category_id"]),
        months_ahead=int(target_raw.get("months_ahead", 8)),
        poll_seconds=float(target_raw.get("poll_seconds", 30)),
        start_date=parse_date(target_raw.get("start_date")),
        end_date=parse_date(target_raw.get("end_date")),
    )

    tg_raw = raw.get("telegram", {})
    telegram = TelegramConfig(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or tg_raw.get("bot_token"),
        chat_id=os.getenv("TELEGRAM_CHAT_ID") or str(tg_raw.get("chat_id") or "") or None,
        allowed_user_id=os.getenv("TELEGRAM_ALLOWED_USER_ID") or str(tg_raw.get("allowed_user_id") or "") or None,
    )

    proxy_server = os.getenv("PROXY_SERVER") or raw.get("proxy", {}).get("server")
    proxy = None
    if proxy_server:
        proxy = {"server": proxy_server}
        username = os.getenv("PROXY_USERNAME") or raw.get("proxy", {}).get("username")
        password = os.getenv("PROXY_PASSWORD") or raw.get("proxy", {}).get("password")
        if username:
            proxy["username"] = username
        if password:
            proxy["password"] = password

    return BotConfig(
        target=target,
        profile=dict(raw.get("profile", {})),
        selectors=dict(raw.get("selectors", {})),
        checkbox_selectors=list(raw.get("checkbox_selectors", [])),
        telegram=telegram,
        user_data_dir=str(raw.get("user_data_dir", ".diplo-profile")),
        headless=bool(raw.get("headless", True)),
        navigation_timeout_ms=int(raw.get("navigation_timeout_ms", 45_000)),
        proxy=proxy,
        exit_after_booking=bool(raw.get("exit_after_booking", True)),
        screenshot_dir=str(raw.get("screenshot_dir", "screenshots")),
    )


def month_sequence(months_ahead: int) -> list[dt.date]:
    today = dt.date.today()
    months: list[dt.date] = []
    for offset in range(months_ahead):
        month_index = today.month - 1 + offset
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        months.append(dt.date(year, month, 1))
    return months


def month_url(target: Target, month: dt.date) -> str:
    params = {
        "locationCode": target.location_code,
        "realmId": target.realm_id,
        "categoryId": target.category_id,
        "dateStr": month.strftime("01.%m.%Y"),
    }
    return f"{DIPLO_ORIGIN}{MONTH_PATH}?{urlencode(params)}"


def day_url(target: Target, day: dt.date) -> str:
    params = {
        "locationCode": target.location_code,
        "realmId": target.realm_id,
        "categoryId": target.category_id,
        "dateStr": day.strftime("%d.%m.%Y"),
    }
    return f"{DIPLO_ORIGIN}{DAY_PATH}?{urlencode(params)}"


def parse_day_from_url(url: str) -> dt.date | None:
    try:
        date_str = parse_qs(urlparse(url).query).get("dateStr", [None])[0]
        return parse_date(date_str)
    except Exception:
        return None


def target_accepts(target: Target, day: dt.date | None) -> bool:
    if day is None:
        return True
    if target.start_date and day < target.start_date:
        return False
    if target.end_date and day > target.end_date:
        return False
    return True


def normalise(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


class TelegramReviewer:
    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(35.0))
        self.offset = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def _api(self, method: str, *, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.cfg.bot_token:
            raise RuntimeError("Telegram bot token is not configured")
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/{method}"
        response = await self.client.post(url, data=data, files=files)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload

    async def send_text(self, text: str) -> None:
        if not self.cfg.enabled:
            print(f"TELEGRAM_DISABLED: {text}")
            return
        await self._api("sendMessage", data={"chat_id": self.cfg.chat_id, "text": text})

    async def ask_for_approval(self, caption: str, screenshot: Path | None) -> str:
        if not self.cfg.enabled:
            print("\n=== REVIEW REQUIRED ===")
            print(caption)
            answer = await asyncio.to_thread(input, "Type BOOK or SKIP: ")
            return "book" if answer.strip().lower() == "book" else "skip"

        nonce = secrets.token_urlsafe(10)
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ BOOK THIS", "callback_data": f"book:{nonce}"},
                {"text": "❌ SKIP", "callback_data": f"skip:{nonce}"},
            ]]
        }
        base_data = {
            "chat_id": self.cfg.chat_id,
            "reply_markup": json.dumps(keyboard),
        }

        if screenshot and screenshot.exists():
            data = dict(base_data)
            data["caption"] = caption[:1024]
            with screenshot.open("rb") as handle:
                await self._api("sendPhoto", data=data, files={"photo": (screenshot.name, handle, "image/png")})
        else:
            data = dict(base_data)
            data["text"] = caption[:4096]
            await self._api("sendMessage", data=data)

        while True:
            result = await self._api(
                "getUpdates",
                data={"timeout": 25, "offset": self.offset, "allowed_updates": json.dumps(["callback_query"])},
            )
            for update in result.get("result", []):
                self.offset = max(self.offset, int(update["update_id"]) + 1)
                callback = update.get("callback_query") or {}
                data = str(callback.get("data") or "")
                sender_id = str((callback.get("from") or {}).get("id") or "")

                if self.cfg.allowed_user_id and sender_id != str(self.cfg.allowed_user_id):
                    if callback.get("id"):
                        await self._api("answerCallbackQuery", data={
                            "callback_query_id": callback["id"],
                            "text": "This approval button is not for your account.",
                            "show_alert": "true",
                        })
                    continue

                expected_book = f"book:{nonce}"
                expected_skip = f"skip:{nonce}"
                if data not in (expected_book, expected_skip):
                    continue

                if callback.get("id"):
                    await self._api("answerCallbackQuery", data={
                        "callback_query_id": callback["id"],
                        "text": "Booking approved" if data == expected_book else "Skipped",
                    })
                return "book" if data == expected_book else "skip"


class DiploBookingBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.reviewer = TelegramReviewer(cfg.telegram)
        self.browser_context: BrowserContext | None = None
        self.screenshot_dir = Path(cfg.screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.last_skipped: dict[str, float] = {}

    async def run(self) -> None:
        async with async_playwright() as pw:
            launch_kwargs: dict[str, Any] = {
                "headless": self.cfg.headless,
                "viewport": {"width": 1365, "height": 900},
                "locale": "en-GB",
                "timezone_id": "Europe/London",
                "accept_downloads": False,
            }
            if self.cfg.proxy:
                launch_kwargs["proxy"] = self.cfg.proxy

            print(f"Launching persistent Chromium profile at {self.cfg.user_data_dir}")
            self.browser_context = await pw.chromium.launch_persistent_context(
                self.cfg.user_data_dir,
                **launch_kwargs,
            )
            self.browser_context.set_default_timeout(15_000)
            self.browser_context.set_default_navigation_timeout(self.cfg.navigation_timeout_ms)

            page = self.browser_context.pages[0] if self.browser_context.pages else await self.browser_context.new_page()
            try:
                await self.poll_forever(page)
            finally:
                await self.reviewer.close()
                await self.browser_context.close()

    async def poll_forever(self, page: Page) -> None:
        months = month_sequence(self.cfg.target.months_ahead)
        index = 0
        await self.reviewer.send_text(
            "RK-Termin watcher started for "
            f"{self.cfg.target.location_code}/{self.cfg.target.realm_id}/{self.cfg.target.category_id}."
        )

        while True:
            month = months[index % len(months)]
            index += 1
            try:
                slot = await self.check_month(page, month)
                if slot:
                    key = slot["day_url"]
                    last_skip = self.last_skipped.get(key, 0)
                    if time.time() - last_skip < 300:
                        print(f"Skipping recently rejected slot: {key}")
                    else:
                        booked = await self.prepare_and_review(slot)
                        if booked and self.cfg.exit_after_booking:
                            print("Booking submitted; exiting because exit_after_booking=true")
                            return
            except PlaywrightError as exc:
                print(f"Playwright error while checking {month:%Y-%m}: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"Unexpected error while checking {month:%Y-%m}: {exc!r}", file=sys.stderr)

            await asyncio.sleep(self.cfg.target.poll_seconds)
            if index % len(months) == 0:
                months = month_sequence(self.cfg.target.months_ahead)

    async def check_month(self, page: Page, month: dt.date) -> dict[str, Any] | None:
        url = month_url(self.cfg.target, month)
        print(f"CHECK {url}")
        response = await page.goto(url, wait_until="domcontentloaded")
        if not response:
            raise RuntimeError("No response returned by browser navigation")

        body = await page.content()
        lowered = body.lower()
        if any(marker in lowered for marker in CAPTCHA_MARKERS):
            await self.reviewer.send_text(
                "Diplo presented a CAPTCHA/session challenge while monitoring. "
                "Open the persistent browser session and complete it; the bot will not bypass it."
            )
            return None

        day_links = await self.extract_day_links(page)
        if not day_links:
            day_links = await self.infer_day_links_from_calendar(page)

        if not day_links:
            page_text = normalise(await page.locator("body").inner_text())
            recognised_empty = any(p in page_text for p in NO_SLOT_PHRASES)
            print(
                f"month={month:%Y-%m} status={response.status} day_links=0 "
                f"recognised_no_slots={recognised_empty}"
            )
            return None

        accepted: list[tuple[dt.date | None, str]] = []
        for link in day_links:
            day = parse_day_from_url(link)
            if target_accepts(self.cfg.target, day):
                accepted.append((day, link))
        accepted.sort(key=lambda item: item[0] or dt.date.max)

        if not accepted:
            print(f"Found {len(day_links)} day links, but none match requested date window")
            return None

        day, link = accepted[0]
        print(f"SLOT_DAY_FOUND day={day} url={link}")
        return {"day": day, "day_url": link}

    async def extract_day_links(self, page: Page) -> list[str]:
        hrefs = await page.locator("a").evaluate_all(
            "els => els.map(a => a.href).filter(Boolean)"
        )
        links: list[str] = []
        for href in hrefs:
            parsed = urlparse(href)
            if not parsed.path.endswith("appointment_showDay.do"):
                continue
            qs = parse_qs(parsed.query)
            if qs.get("locationCode", [self.cfg.target.location_code])[0] != self.cfg.target.location_code:
                continue
            if qs.get("realmId", [self.cfg.target.realm_id])[0] != self.cfg.target.realm_id:
                continue
            if qs.get("categoryId", [self.cfg.target.category_id])[0] != self.cfg.target.category_id:
                continue
            if href not in links:
                links.append(href)
        return links

    async def infer_day_links_from_calendar(self, page: Page) -> list[str]:
        """Compatibility path for older RK-Termin pages used by terminwatch.

        Old layouts expose dates as h4 headings and availability text in the next
        sibling rather than linking directly to appointment_showDay.do.
        """
        rows = await page.locator("h4").evaluate_all(
            """els => els.map(h => ({
                heading: (h.innerText || '').trim(),
                sibling: ((h.parentElement && h.parentElement.nextElementSibling)
                    ? h.parentElement.nextElementSibling.innerText : '') || ''
            }))"""
        )
        found: list[str] = []
        for row in rows:
            heading = str(row.get("heading") or "")
            sibling = normalise(str(row.get("sibling") or ""))
            if any(p in sibling for p in NO_SLOT_PHRASES):
                continue
            match = re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b", heading)
            if not match:
                continue
            try:
                day = parse_date(match.group(1))
            except ValueError:
                continue
            if day and target_accepts(self.cfg.target, day):
                found.append(day_url(self.cfg.target, day))
        return found

    async def prepare_and_review(self, slot: dict[str, Any]) -> bool:
        assert self.browser_context is not None
        booking_page = await self.browser_context.new_page()
        booking_page.set_default_navigation_timeout(self.cfg.navigation_timeout_ms)

        try:
            await booking_page.goto(slot["day_url"], wait_until="domcontentloaded")
            interruption = await self.detect_interruption(booking_page)
            if interruption:
                await self.reviewer.send_text(
                    f"Slot found for {slot.get('day')}, but {interruption} is required before the form can continue. "
                    f"URL: {booking_page.url}"
                )
                return False

            booking_link = await self.choose_booking_link(booking_page)
            if booking_link:
                print(f"Opening booking form candidate: {booking_link}")
                await booking_page.goto(booking_link, wait_until="domcontentloaded")
            else:
                print("No separate booking link found; treating current day page as possible form page")

            interruption = await self.detect_interruption(booking_page)
            if interruption:
                await self.reviewer.send_text(
                    f"Slot found for {slot.get('day')}, but {interruption} is required on the booking page. "
                    f"URL: {booking_page.url}"
                )
                return False

            fill_report = await self.fill_form(booking_page)
            submit = await self.find_submit_control(booking_page)
            if submit is None:
                screenshot = await self.take_screenshot(booking_page, "no-submit")
                await self.reviewer.send_text(
                    "A slot was found and the form was opened, but I could not identify a final submit button. "
                    f"Autofilled fields: {', '.join(fill_report['filled']) or 'none'}. "
                    f"URL: {booking_page.url}. Screenshot: {screenshot}"
                )
                return False

            screenshot = await self.take_screenshot(booking_page, "review")
            missing = ", ".join(fill_report["missing"]) or "none"
            filled = ", ".join(fill_report["filled"]) or "none"
            caption = (
                "German Embassy London appointment ready for review\n\n"
                f"Date: {slot.get('day') or 'detected slot'}\n"
                f"URL: {booking_page.url}\n"
                f"Autofilled: {filled}\n"
                f"Not matched: {missing}\n\n"
                "BOOK THIS will click the final submit control in this exact browser session."
            )
            decision = await self.reviewer.ask_for_approval(caption, screenshot)

            if decision != "book":
                self.last_skipped[slot["day_url"]] = time.time()
                print("Reviewer skipped slot")
                return False

            # Re-check for last-second CAPTCHA/OTP before the irreversible click.
            interruption = await self.detect_interruption(booking_page)
            if interruption:
                await self.reviewer.send_text(
                    f"Approval received, but final submission paused because {interruption} appeared. "
                    f"URL: {booking_page.url}"
                )
                return False

            print("APPROVED: clicking final submit control")
            await submit.click()
            try:
                await booking_page.wait_for_load_state("domcontentloaded", timeout=20_000)
            except PlaywrightError:
                pass

            result_shot = await self.take_screenshot(booking_page, "submitted")
            text = normalise(await booking_page.locator("body").inner_text())[:1600]
            await self.reviewer.send_text(
                "BOOK action sent.\n"
                f"Result URL: {booking_page.url}\n"
                f"Page excerpt: {text[:900]}\n"
                f"Screenshot saved: {result_shot}"
            )
            return True
        finally:
            if not self.cfg.exit_after_booking:
                await booking_page.close()

    async def choose_booking_link(self, page: Page) -> str | None:
        candidates = await page.locator("a").evaluate_all(
            "els => els.map(a => ({href: a.href || '', text: (a.innerText || '').trim()}))"
        )
        scored: list[tuple[int, str]] = []
        for item in candidates:
            href = str(item.get("href") or "")
            text = normalise(str(item.get("text") or ""))
            if not href.startswith(DIPLO_ORIGIN):
                continue
            parsed = urlparse(href)
            path_lower = parsed.path.lower()
            if path_lower.endswith("appointment_showmonth.do") or path_lower.endswith("appointment_showday.do"):
                continue

            score = 0
            combined = f"{path_lower}?{parsed.query}".lower()
            if any(hint in combined for hint in BOOKING_URL_HINTS):
                score += 120
            if any(word in text for word in SUBMIT_WORDS):
                score += 80
            if "appointment" in path_lower:
                score += 30
            if "dateStr=" in href or "datestr=" in href.lower():
                score += 10
            if score:
                scored.append((score, href))

        if not scored:
            return None
        scored.sort(reverse=True)
        return scored[0][1]

    async def detect_interruption(self, page: Page) -> str | None:
        html = normalise(await page.content())
        if any(marker in html for marker in CAPTCHA_MARKERS):
            return "a CAPTCHA/session challenge"
        body_text = normalise(await page.locator("body").inner_text())
        if any(marker in body_text for marker in OTP_MARKERS):
            return "an OTP/verification-code step"
        return None

    async def fill_form(self, page: Page) -> dict[str, list[str]]:
        filled: list[str] = []
        missing: list[str] = []

        for key, value in self.cfg.profile.items():
            if value is None or value == "":
                continue
            locator = await self.locate_profile_control(page, key)
            if locator is None:
                missing.append(key)
                continue
            try:
                await self.fill_control(locator, str(value))
                filled.append(key)
            except Exception as exc:
                print(f"Could not fill {key}: {exc!r}")
                missing.append(key)

        for selector in self.cfg.checkbox_selectors:
            try:
                checkbox = page.locator(selector).first
                if await checkbox.count() and not await checkbox.is_checked():
                    await checkbox.check()
            except Exception as exc:
                print(f"Could not check {selector}: {exc!r}")

        return {"filled": filled, "missing": missing}

    async def locate_profile_control(self, page: Page, key: str) -> Locator | None:
        explicit = self.cfg.selectors.get(key)
        if explicit:
            loc = page.locator(explicit).first
            if await loc.count():
                return loc

        synonyms = tuple({key.replace("_", " "), key, *DEFAULT_SYNONYMS.get(key, ())})
        descriptors = await page.locator("input, textarea, select").evaluate_all(
            """els => els.map((el, i) => {
                const id = el.id || '';
                let label = '';
                if (id) {
                    const l = document.querySelector('label[for="' + CSS.escape(id) + '"]');
                    if (l) label = l.innerText || '';
                }
                if (!label && el.closest('label')) label = el.closest('label').innerText || '';
                return {
                    i,
                    type: (el.type || el.tagName || '').toLowerCase(),
                    name: el.name || '',
                    id,
                    placeholder: el.placeholder || '',
                    aria: el.getAttribute('aria-label') || '',
                    autocomplete: el.getAttribute('autocomplete') || '',
                    label
                };
            })"""
        )

        best: tuple[int, int] | None = None
        for item in descriptors:
            type_name = normalise(str(item.get("type") or ""))
            if type_name in {"hidden", "submit", "button", "reset", "image", "file", "checkbox", "radio"}:
                continue
            haystack = normalise(" ".join(str(item.get(k) or "") for k in ("name", "id", "placeholder", "aria", "autocomplete", "label")))
            score = 0
            for synonym in synonyms:
                syn = normalise(synonym)
                if not syn:
                    continue
                if haystack == syn:
                    score = max(score, 120)
                elif re.search(rf"\b{re.escape(syn)}\b", haystack):
                    score = max(score, 90)
                elif syn in haystack:
                    score = max(score, 60)
            if score and (best is None or score > best[0]):
                best = (score, int(item["i"]))

        if best is None:
            return None
        return page.locator("input, textarea, select").nth(best[1])

    async def fill_control(self, locator: Locator, value: str) -> None:
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        input_type = normalise(await locator.get_attribute("type"))
        if tag == "select":
            # Try value, exact label, then a case-insensitive partial label.
            try:
                await locator.select_option(value=value)
                return
            except Exception:
                pass
            try:
                await locator.select_option(label=value)
                return
            except Exception:
                pass
            options = await locator.locator("option").evaluate_all(
                "els => els.map(o => ({value: o.value, label: (o.innerText || '').trim()}))"
            )
            for option in options:
                if normalise(value) in normalise(str(option.get("label") or "")):
                    await locator.select_option(value=str(option.get("value") or ""))
                    return
            raise RuntimeError(f"No select option matched {value!r}")

        if input_type in {"date"}:
            parsed = parse_date(value)
            await locator.fill(parsed.isoformat() if parsed else value)
            return
        await locator.fill(value)

    async def find_submit_control(self, page: Page) -> Locator | None:
        explicit = self.cfg.selectors.get("submit")
        if explicit:
            loc = page.locator(explicit).first
            if await loc.count():
                return loc

        candidates = page.locator("button, input[type=submit], input[type=button]")
        count = await candidates.count()
        best: tuple[int, int] | None = None
        for i in range(count):
            loc = candidates.nth(i)
            try:
                if not await loc.is_visible() or not await loc.is_enabled():
                    continue
                text = normalise(await loc.inner_text())
                value = normalise(await loc.get_attribute("value"))
                name = normalise(await loc.get_attribute("name"))
                ident = normalise(await loc.get_attribute("id"))
            except Exception:
                continue
            haystack = " ".join((text, value, name, ident))
            score = 0
            for word in SUBMIT_WORDS:
                if normalise(word) in haystack:
                    score = max(score, 80)
            if "submit" in haystack or "book" in haystack or "buchen" in haystack:
                score += 40
            if score and (best is None or score > best[0]):
                best = (score, i)

        if best is None:
            return None
        return candidates.nth(best[1])

    async def take_screenshot(self, page: Page, label: str) -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.screenshot_dir / f"{stamp}-{label}.png"
        await page.screenshot(path=str(path), full_page=True)
        return path


async def amain() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DIPLO_CONFIG", "config.json")
    cfg = load_config(config_path)
    bot = DiploBookingBot(cfg)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
