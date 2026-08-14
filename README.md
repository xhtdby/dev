# RK-Termin review-gated booking bot

A Diplo-specific appointment watcher that combines:

- **Terminwatch-style discovery** for `appointment_showMonth.do` / `appointment_showDay.do`.
- **VFS-auto-style browser automation**: persistent Chromium session, proxy support, notifications and form interaction.
- **Human approval before final submit**: the bot fills the booking form, sends a screenshot to Telegram, and only clicks the final submit control after you press **BOOK THIS**.

The default example is preconfigured for the London route from the original request:

- `locationCode=lond`
- `realmId=1614`
- `categoryId=4019`

## Flow

```text
poll one Diplo month
        |
        v
bookable day detected
        |
        v
open appointment_showDay.do
        |
        v
open best booking/form link
        |
        v
autofill applicant fields
        |
        v
screenshot + Telegram review card
        |
        +---- SKIP ----> return to monitoring
        |
        +---- BOOK THIS
                  |
                  v
          click final submit
                  |
                  v
          result notification
```

The process deliberately stops if it encounters a CAPTCHA or OTP/verification-code step rather than attempting to solve or bypass it.

## Files

- `diplo_bot.py` — watcher, booking navigator, autofill and approval gate.
- `config.example.json` — London category 4019 target plus applicant-field template.
- `Dockerfile` — Chromium + Python runtime.
- `docker-compose.yml` — VPS deployment with a persistent browser-profile volume.

## 1. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with the applicant details you want the bot to prefill.

For fields that the generic matcher does not identify correctly, add exact CSS selectors:

```json
{
  "selectors": {
    "first_name": "#firstName",
    "passport_number": "input[name='passport']",
    "submit": "button[type='submit']"
  }
}
```

`submit` is special: the bot identifies it but **does not click it** until the Telegram approval arrives.

You can restrict acceptable appointment dates:

```json
{
  "target": {
    "start_date": "2026-08-15",
    "end_date": "2026-09-30"
  }
}
```

## 2. Telegram approval button

Create a Telegram bot with BotFather and set these environment variables in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456:abc...
TELEGRAM_CHAT_ID=123456789
TELEGRAM_ALLOWED_USER_ID=123456789
```

`TELEGRAM_ALLOWED_USER_ID` prevents somebody else who can see a forwarded/reused callback from approving the booking.

When a form is ready, the bot sends a screenshot with two inline buttons:

- `✅ BOOK THIS`
- `❌ SKIP`

Without Telegram configuration, an interactive terminal prompt is used instead.

## 3. Residential/ISP proxy

The browser accepts a Playwright proxy via environment variables:

```dotenv
PROXY_SERVER=http://host:port
PROXY_USERNAME=user
PROXY_PASSWORD=password
```

Do not put proxy or Telegram credentials in `config.json` if the repository is public.

## 4. Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp config.example.json config.json
python diplo_bot.py config.json
```

For initial selector discovery, setting `"headless": false` is useful.

## 5. Run on a VPS

Install Docker and Docker Compose, then:

```bash
cp config.example.json config.json
# edit config.json and .env
docker compose up -d --build
docker compose logs -f
```

The `diplo-browser-profile` Docker volume survives container restarts, preserving Chromium cookies/local storage. Screenshots are written to `./screenshots` on the VPS.

## Polling behaviour

The bot rotates through months rather than hitting all months at once. With 8 months and `poll_seconds: 30`, Diplo receives one navigation every ~30 seconds while each individual month is revisited roughly every 4 minutes.

When a day link appears, polling pauses while the exact browser session is taken through the form and held for review.

## Autofill strategy

The generic matcher scores visible form controls using their:

- label
- `name`
- `id`
- placeholder
- ARIA label
- autocomplete metadata

It understands common English/German labels for first name, surname, email, phone, passport number, DOB, nationality and address fields. Explicit selectors in `config.json` always win.

## Adapting after the first real slot

The first actual booking form is the important calibration event. If the Telegram card reports fields under `Not matched`, inspect the saved screenshot/DOM and add exact selectors to `config.json`. The bot's final-submit gate is intentionally selector-overridable for the same reason.

## Origins

The month/day detection model is based on the long-running `terminwatch.py` approach for the German Foreign Office RK-Termin portal. The browser/proxy/notification/automatic-interaction architecture is inspired by VFS appointment automation projects such as `barrriwa/vfsauto`, but this implementation uses Playwright and inserts a mandatory final human approval gate.
