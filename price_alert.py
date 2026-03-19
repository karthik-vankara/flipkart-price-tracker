import json
import os
import re
import sys
import random
import smtplib
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from dotenv import load_dotenv
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Load .env when running locally (no-op in GitHub Actions where secrets are
# already injected as real environment variables)
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
PRODUCTS_FILE = SCRIPT_DIR / "products.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

PRICE_SELECTORS = [
    # Current Flipkart (2025-26) class names — inspected live
    "div._1psv1zeb9",
    "div.v1zwn21k",
    # Previous Flipkart class names (kept as fallbacks)
    "div.Nx9bqj.CxhGGd",
    "div._30jeq3._16Jk6d",
    "div._30jeq3",
    "div[class*='price']",
]


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _apply_pincode(page, pincode: str) -> None:
    """
    Enter the delivery pincode on the Flipkart product page so that the price
    shown reflects the user's actual location.
    """
    try:
        # Flipkart pincode input — try multiple known selectors
        input_selectors = [
            'input._2KpZ6l',
            'input[placeholder*="incode"]',
            'input[class*="pincode"]',
        ]
        input_el = None
        for sel in input_selectors:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=2_000):
                input_el = loc
                break

        if input_el is None:
            print(f"  [pincode] WARN: Pincode input not found — price may reflect default location.")
            return

        input_el.click()
        input_el.fill(pincode)
        input_el.press("Enter")
        # Wait for Flipkart to reload the price after pincode is applied
        page.wait_for_load_state("networkidle", timeout=10_000)
        time.sleep(1.5)
        print(f"  [pincode] Applied pincode {pincode}.")
    except Exception as exc:
        print(f"  [pincode] WARN: Could not apply pincode — {exc}")


def scrape_price(url: str, pincode: str | None = None) -> float | None:
    """
    Launch a headless Chromium browser, load the Flipkart product page,
    optionally set a delivery pincode, and extract the current price.

    Returns the price as a float, or None if extraction fails.
    """
    print(f"  [scrape] URL: {url}")
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            try:
                page.goto(url, timeout=30_000, wait_until="networkidle")
            except PlaywrightTimeoutError:
                print("  [scrape] ERROR: Page load timed out after 30 seconds.")
                return None

            # Polite delay to let dynamic content settle
            time.sleep(random.uniform(2, 5))

            # Apply pincode so price reflects the correct delivery location
            if pincode:
                _apply_pincode(page, pincode)

            # --- Strategy 1: Playwright locators (visibility-aware) ----------
            # This avoids grabbing prices from off-screen / related-products
            # carousels, because Playwright only sees rendered visible elements.
            raw_text = None
            for selector in PRICE_SELECTORS:
                try:
                    locator = page.locator(selector).first
                    if locator.count() and locator.is_visible(timeout=2_000):
                        candidate_text = locator.inner_text().strip()
                        candidate_clean = candidate_text.replace("₹", "").replace(",", "").strip()
                        candidate_match = re.search(r"\d+(?:\.\d+)?", candidate_clean)
                        if candidate_match and float(candidate_match.group()) >= 100:
                            raw_text = candidate_text
                            print(f"  [scrape] Playwright locator '{selector}' → {raw_text!r}")
                            break
                        else:
                            print(f"  [scrape] Playwright locator '{selector}' skipped → {candidate_text!r} (too low or no number)")
                except Exception:
                    continue

            # --- Strategy 2: BeautifulSoup on full HTML (fallback) -----------
            if raw_text is None:
                html = page.content()
                soup = BeautifulSoup(html, "lxml")

                for selector in PRICE_SELECTORS:
                    element = soup.select_one(selector)
                    if element:
                        candidate_text = element.get_text(strip=True)
                        candidate_clean = candidate_text.replace("₹", "").replace(",", "").strip()
                        candidate_match = re.search(r"\d+(?:\.\d+)?", candidate_clean)
                        if candidate_match and float(candidate_match.group()) >= 100:
                            raw_text = candidate_text
                            print(f"  [scrape] BS4 selector '{selector}' → {raw_text!r}")
                            break
                        elif candidate_match:
                            print(f"  [scrape] BS4 selector '{selector}' skipped → value {candidate_match.group()} too low")

                # --- Strategy 3: ₹-symbol scan, pick price closest to ₹1k-₹1L range -
                if raw_text is None:
                    candidates = []
                    for tag in soup.find_all(True):
                        text = tag.get_text(strip=True)
                        if re.fullmatch(r"₹[\d,]+(?:\.\d+)?", text):
                            numeric = float(text.replace("₹", "").replace(",", ""))
                            if 100 <= numeric <= 10_00_000:
                                candidates.append((numeric, text, tag))
                    if candidates:
                        # De-duplicate by value, then pick the highest frequency value
                        # (main price usually appears in multiple spots; related items once)
                        from collections import Counter
                        freq = Counter(round(v, -1) for v, _, _ in candidates)
                        dominant = freq.most_common(1)[0][0]
                        dominant_candidates = [(v, t, e) for v, t, e in candidates
                                               if abs(round(v, -1) - dominant) < 50]
                        dominant_candidates.sort(key=lambda x: x[0])
                        best_numeric, raw_text, best_tag = dominant_candidates[0]
                        print(f"  [scrape] ₹-fallback all candidates: {sorted(set(c[1] for c in candidates))}")
                        print(f"  [scrape] Picked (most-frequent bucket): {raw_text!r}")

            if raw_text is None:
                print("  [scrape] WARN: No price element matched any known selector.")
                return None

            # Strip currency symbol, commas, whitespace
            cleaned = raw_text.replace("₹", "").replace(",", "").strip()
            match = re.search(r"\d+(?:\.\d+)?", cleaned)
            if not match:
                print(f"  [scrape] WARN: Could not extract numeric value from {cleaned!r}")
                return None

            price = float(match.group())
            print(f"  [scrape] Parsed price: {price}")
            return price

    except PlaywrightTimeoutError:
        print("  [scrape] ERROR: Playwright timed out.")
        return None
    except Exception as exc:
        print(f"  [scrape] ERROR: Unexpected exception — {exc}")
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _build_email_body(alerts: list[dict]) -> tuple[str, str]:
    """Return (subject, plain-text body) for the given alert list."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = (
        f"Flipkart Price Alert — {len(alerts)} product(s) below threshold | {now}"
    )
    lines = ["The following Flipkart products are now below your price threshold:", ""]
    for idx, item in enumerate(alerts, start=1):
        lines.append(f"Product   : {item['name']}")
        lines.append(f"Price     : \u20b9{item['current_price']:,.0f}")
        lines.append(f"Threshold : \u20b9{item['threshold']:,.0f}")
        lines.append(f"Buy now   : {item['url']}")
        if idx < len(alerts):
            lines.append("-" * 60)
        lines.append("")
    return subject, "\n".join(lines)


def _send_via_gmail(subject: str, body: str) -> None:
    """Send using Gmail SMTP + App Password."""
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_PASS"]
    alert_email = os.environ["ALERT_EMAIL"]

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = gmail_user
    message["To"] = alert_email
    message.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, alert_email, message.as_string())
    print("Email sent successfully via Gmail SMTP.")


def _send_via_sendgrid(subject: str, body: str) -> None:
    """Send using the SendGrid Web API v3 (no extra library needed)."""
    api_key = os.environ["SENDGRID_API_KEY"]
    sender = os.environ["SENDGRID_FROM"]
    alert_email = os.environ["ALERT_EMAIL"]

    payload = json.dumps({
        "personalizations": [{"to": [{"email": alert_email}]}],
        "from": {"email": sender},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }).encode()

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        print(f"Email sent successfully via SendGrid (HTTP {resp.status}).")


def _send_via_resend(subject: str, body: str) -> None:
    """Send using the Resend API v1 (resend.com — free 3,000 emails/month)."""
    api_key = os.environ["RESEND_API_KEY"]
    sender = os.environ["RESEND_FROM"]   # e.g. "Flipkart Alert <you@yourdomain.com>"
    alert_email = os.environ["ALERT_EMAIL"]

    payload = json.dumps({
        "from": sender,
        "to": [alert_email],
        "subject": subject,
        "text": body,
    }).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Email sent successfully via Resend (HTTP {resp.status}).")
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        print(f"  [resend] HTTP {e.code} {e.reason}")
        print(f"  [resend] Response body: {body_bytes.decode(errors='replace')}")
        raise


def _send_via_outlook(subject: str, body: str) -> None:
    """Send using Outlook/Hotmail SMTP (no App Password — regular password works)."""
    outlook_user = os.environ["OUTLOOK_USER"]   # yourname@outlook.com / @hotmail.com
    outlook_pass = os.environ["OUTLOOK_PASS"]   # your normal Outlook password
    alert_email = os.environ["ALERT_EMAIL"]

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = outlook_user
    message["To"] = alert_email
    message.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp-mail.outlook.com", 587) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(outlook_user, outlook_pass)
        server.sendmail(outlook_user, alert_email, message.as_string())
    print("Email sent successfully via Outlook SMTP.")


def send_alert(alerts: list[dict]) -> None:
    """
    Send an alert email. Backend is chosen automatically by which env vars are set:

      RESEND_API_KEY   → Resend API   (recommended: free, no domain needed for testing)
      SENDGRID_API_KEY → SendGrid API
      OUTLOOK_USER     → Outlook/Hotmail SMTP (no App Password needed)
      GMAIL_USER       → Gmail SMTP  (requires a Gmail App Password)
    """
    subject, body = _build_email_body(alerts)

    try:
        if os.environ.get("RESEND_API_KEY"):
            print("  [email] Using Resend backend.")
            _send_via_resend(subject, body)
        elif os.environ.get("SENDGRID_API_KEY"):
            print("  [email] Using SendGrid backend.")
            _send_via_sendgrid(subject, body)
        elif os.environ.get("OUTLOOK_USER"):
            print("  [email] Using Outlook SMTP backend.")
            _send_via_outlook(subject, body)
        else:
            print("  [email] Using Gmail SMTP backend.")
            _send_via_gmail(subject, body)
    except Exception as exc:
        print(f"ERROR: Failed to send email — {exc}")
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== Flipkart Price Alert Run: {now} ===")

    # Load products
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    products = data["products"]

    # Validate required environment variables.
    # Backend priority: Resend → SendGrid → Outlook → Gmail
    required_always = ["ALERT_EMAIL"]
    if os.environ.get("RESEND_API_KEY"):
        required_backend = ["RESEND_API_KEY", "RESEND_FROM"]
    elif os.environ.get("SENDGRID_API_KEY"):
        required_backend = ["SENDGRID_API_KEY", "SENDGRID_FROM"]
    elif os.environ.get("OUTLOOK_USER"):
        required_backend = ["OUTLOOK_USER", "OUTLOOK_PASS"]
    else:
        required_backend = ["GMAIL_USER", "GMAIL_PASS"]
    missing = [v for v in required_always + required_backend if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variable(s): {', '.join(missing)}")
        sys.exit(1)

    alerts: list[dict] = []

    for i, product in enumerate(products):
        name = product["name"]
        url = product["url"]
        threshold = float(product["threshold"])

        pincode = product.get("pincode")

        print(f"\nChecking: {name}" + (f" (pincode: {pincode})" if pincode else ""))

        try:
            current_price = scrape_price(url, pincode=pincode)
        except Exception as exc:
            print(f"  WARN: Unhandled error while scraping '{name}': {exc}")
            current_price = None

        if current_price is None:
            print(f"  WARN: Could not scrape price for {name}, skipping.")
        else:
            print(f"  ----------------------------------------")
            print(f"  Product   : {name}")
            print(f"  Scraped   : ₹{current_price:,.0f}")
            print(f"  Threshold : ₹{threshold:,.0f}")
            print(f"  Verdict   : {'BELOW threshold — ALERT!' if current_price < threshold else 'Above threshold — OK'}")
            print(f"  ----------------------------------------")
            if current_price < threshold:
                alerts.append(
                    {
                        "name": name,
                        "url": url,
                        "current_price": current_price,
                        "threshold": threshold,
                    }
                )

        # Polite delay between products (skip after last product)
        if i < len(products) - 1:
            delay = random.uniform(3, 7)
            print(f"  Waiting {delay:.1f}s before next product...")
            time.sleep(delay)

    print()
    if alerts:
        print(f"Sending alert email for {len(alerts)} product(s)...")
        try:
            send_alert(alerts)
        except Exception as exc:
            print(f"ERROR: Email sending failed — {exc}")
    else:
        print("No alerts triggered. All prices are above thresholds.")

    print("=== Run complete ===")


if __name__ == "__main__":
    main()
