"""
IPTVV Canada - Automated Trial Account Creation

Automates the IPTVV.ca cart checkout flow using temporary mail.tm emails
and extracts Xtream credentials from the received email.

Install deps: pip install selenium webdriver-manager 2captcha-python python-dotenv requests
"""
import argparse
import html
import json
import os
import random
import re
import string
import subprocess
import time
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from twocaptcha import TwoCaptcha
from webdriver_manager.chrome import ChromeDriverManager

import iboplayer_auth

load_dotenv()

try:
    from telegram_notifier import notifier
except ImportError:
    class DummyNotifier:
        def notify_success(self, *args, **kwargs):
            return False

        def notify_error(self, *args, **kwargs):
            return False

    notifier = DummyNotifier()

TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
IPTVV_BASE_URL = os.getenv("IPTVV_BASE_URL", "https://iptvv.ca").rstrip("/")
IPTVV_CART_URL = os.getenv("IPTVV_CART_URL", f"{IPTVV_BASE_URL}/cart/")
MAILTM_API_BASE = os.getenv("MAILTM_API_BASE", "https://api.mail.tm")
EMAIL_POLL_SECONDS = int(os.getenv("IPTVV_EMAIL_POLL_SECONDS", "30"))
EMAIL_MAX_WAIT_SECONDS = int(os.getenv("IPTVV_EMAIL_MAX_WAIT_SECONDS", "2700"))  # 45 minutes

# Email backend:
#   "procmail" (default) — api.procmail.xyz REST API (powers 8gwifi.org/temp-email.jsp).
#                Pure HTTP: GET /generate for an address, GET /inbox/{addr} for messages.
#   "mailtm"             — legacy mail.tm REST API (kept as a fallback).
IPTVV_EMAIL_BACKEND = os.getenv("IPTVV_EMAIL_BACKEND", "procmail").strip().lower()
PROCMAIL_API_BASE = os.getenv("PROCMAIL_API_BASE", "https://api.procmail.xyz").rstrip("/")
AUTO_EXIT = os.getenv("AUTO_EXIT", "True").lower() == "true"
IPTVV_PAGE_LOAD_RETRIES = int(os.getenv("IPTVV_PAGE_LOAD_RETRIES", "2"))
IPTVV_TRIAL_PRODUCT_ID = os.getenv("IPTVV_TRIAL_PRODUCT_ID", "7758")
IPTVV_CLOUDFLARE_WAIT_SECONDS = int(os.getenv("IPTVV_CLOUDFLARE_WAIT_SECONDS", "45"))
IPTVV_DEBUG_DIR = os.getenv("IPTVV_DEBUG_DIR", "/app/logs")
IPTVV_PROXY_CHECK_URL = os.getenv("IPTVV_PROXY_CHECK_URL", "https://api.ipify.org")
IPTVV_KNOWN_BLOCKED_IP = os.getenv("IPTVV_KNOWN_BLOCKED_IP", "").strip()

# IBO Player integration configuration
IPTVV_IBOPLAYER_ENABLED = os.getenv("IPTVV_IBOPLAYER_ENABLED", "False").lower() == "true"
IPTVV_IBOPLAYER_COOKIE = os.getenv("IPTVV_IBOPLAYER_COOKIE", "")  # legacy; no longer used for auth
# Device-login credentials (bearer-token auth, replacing the cookie). Fall back to
# the shared IBOPLAYER_* device identity when the IPTVV-specific vars are unset.
IPTVV_IBOPLAYER_MAC_ADDRESS = os.getenv("IPTVV_IBOPLAYER_MAC_ADDRESS") or os.getenv("IBOPLAYER_MAC_ADDRESS", "")
IPTVV_IBOPLAYER_DEVICE_KEY = os.getenv("IPTVV_IBOPLAYER_DEVICE_KEY") or os.getenv("IBOPLAYER_DEVICE_KEY", "")
IPTVV_IBOPLAYER_PLAYLIST_URL_ID = os.getenv("IPTVV_IBOPLAYER_PLAYLIST_URL_ID", "")
IPTVV_IBOPLAYER_PLAYLIST_NAME = os.getenv("IPTVV_IBOPLAYER_PLAYLIST_NAME", "IPTVV Canada")
IPTVV_IBOPLAYER_PLAYLIST_URL_TEMPLATE = os.getenv("IPTVV_IBOPLAYER_PLAYLIST_URL", "http://iptvvcanada.com")

# Email subject patterns to detect credentials email (checked case-insensitively)
CREDENTIALS_EMAIL_SUBJECTS = [
    "Free 24-Hour IPTV Trial Subscription",  # Current IPTVV format
    "Your trial is now active",               # Legacy/alternate format
    "IPTV Trial Subscription",                # Partial match
]
solver = TwoCaptcha(TWOCAPTCHA_API_KEY) if TWOCAPTCHA_API_KEY else None


class CloudflareBlockedError(RuntimeError):
    """Raised when IPTVV serves a Cloudflare/WAF page instead of checkout."""


class TrialRejectedError(RuntimeError):
    """Raised when IPTVV accepts checkout but refuses to issue trial credentials."""


# ═══════════════════════════════════════════════════════════
# Mail.tm API Helper Functions
# ═══════════════════════════════════════════════════════════

def get_available_domains():
    """Fetch list of available mail.tm domains."""
    try:
        response = requests.get(f"{MAILTM_API_BASE}/domains", timeout=10)
        response.raise_for_status()
        domains = response.json()
        if domains and "hydra:member" in domains:
            return [d["domain"] for d in domains["hydra:member"]]
        return []
    except Exception as exc:
        print(f"[!] Failed to fetch mail.tm domains: {exc}")
        return []


def create_mailtm_account():
    """
    Create a temporary email account via mail.tm API.

    Returns:
        tuple: (email_address, password, auth_token) or (None, None, None) on failure
    """
    try:
        domains = get_available_domains()
        if not domains:
            raise RuntimeError("No mail.tm domains available")

        # Generate random email
        username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        domain = random.choice(domains)
        email_address = f"{username}@{domain}"
        password = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

        print(f"[*] Creating mail.tm account: {email_address}")

        # Create account
        create_response = requests.post(
            f"{MAILTM_API_BASE}/accounts",
            json={"address": email_address, "password": password},
            timeout=10
        )
        create_response.raise_for_status()
        print(f"[OK] Mail.tm account created: {email_address}")

        # Get auth token
        token_response = requests.post(
            f"{MAILTM_API_BASE}/token",
            json={"address": email_address, "password": password},
            timeout=10
        )
        token_response.raise_for_status()
        auth_token = token_response.json().get("token")

        if not auth_token:
            raise RuntimeError("Failed to get auth token from mail.tm")

        print("[OK] Mail.tm authentication successful")
        return email_address, password, auth_token

    except Exception as exc:
        print(f"[!] Failed to create mail.tm account: {exc}")
        return None, None, None


def get_mailtm_messages(auth_token):
    """
    Fetch messages from mail.tm inbox.

    Args:
        auth_token: Bearer token for authentication

    Returns:
        list: List of message objects
    """
    try:
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(
            f"{MAILTM_API_BASE}/messages",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return data.get("hydra:member", [])
    except Exception as exc:
        print(f"[!] Failed to fetch mail.tm messages: {exc}")
        return []


def get_mailtm_message_by_id(auth_token, message_id):
    """
    Fetch a specific message's full content from mail.tm.

    Args:
        auth_token: Bearer token for authentication
        message_id: ID of the message to retrieve

    Returns:
        dict: Message object with 'text' and 'html' fields, or None on failure
    """
    try:
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(
            f"{MAILTM_API_BASE}/messages/{message_id}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"[!] Failed to fetch message {message_id}: {exc}")
        return None


def _scan_messages_for_credentials(messages, fetch_full):
    """Scan a batch of inbox messages for the IPTVV credentials (or rejection) email.

    Args:
        messages: list of message summaries, each a dict with at least
                  'subject', 'from': {'address': ...} and 'id'.
        fetch_full: callable(msg) -> full message body dict with 'text'/'html'.
                    For mail.tm this fetches the body by id; for browser backends
                    whose messages are already complete it can return msg unchanged.

    Returns:
        The full credentials message dict, or None if not present in this batch.

    Raises:
        TrialRejectedError: if a rejection email from IPTVV is seen.
    """
    rejection_markers = [
        "already used",
        "duplicate trial",
        "trial was already used",
        "order was cancelled",
        "order was canceled",
    ]

    for msg in messages:
        subject = msg.get("subject", "")
        from_addr = msg.get("from", {}).get("address", "")

        print(f"    - From: {from_addr}, Subject: {subject}")

        lowered_subject = subject.lower()

        if "iptvv" in from_addr.lower() and any(marker in lowered_subject for marker in rejection_markers):
            full_message = fetch_full(msg)
            preview = ""
            if full_message:
                preview = full_message.get("text", "")[:500].strip()
            raise TrialRejectedError(
                f"IPTVV refused to issue trial credentials: {subject}. "
                f"Message preview: {preview}"
            )

        # Check if this is the credentials email (check multiple subject patterns)
        for pattern in CREDENTIALS_EMAIL_SUBJECTS:
            if pattern.lower() in lowered_subject:
                print("[OK] Credentials email found!")
                full_message = fetch_full(msg)
                if full_message:
                    return full_message
                break

    return None


def _wait_for_credentials_email_mailtm(auth_token, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """
    Poll mail.tm inbox until credentials email arrives.

    Args:
        auth_token: Bearer token for authentication
        max_wait_seconds: Maximum time to wait (default: 2700 seconds / 45 minutes)

    Returns:
        dict: Full message object with credentials, or None if timeout
    """
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking mail.tm inbox (attempt {attempt}, {remaining}s remaining)...")

        messages = get_mailtm_messages(auth_token)
        result = _scan_messages_for_credentials(
            messages, fetch_full=lambda m: get_mailtm_message_by_id(auth_token, m["id"])
        )
        if result:
            return result

        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty")

        print(f"[*] Waiting {EMAIL_POLL_SECONDS}s before next check...")
        time.sleep(EMAIL_POLL_SECONDS)

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# procmail.xyz (8gwifi.org) email backend — pure REST API
#
# 8gwifi.org/temp-email.jsp is a thin front-end over api.procmail.xyz:
#   GET /generate         -> plain-text address, e.g. "u6cvh398@goodbanners.xyz"
#   GET /inbox/{address}  -> JSON array of {Sender, Subject, ReceivedAt,
#                            PlainTextBody, HTMLBody}, or null when the inbox is
#                            empty. Bodies are returned inline.
# No browser, bot wall, or auth required, so we poll it directly with requests.
# ═══════════════════════════════════════════════════════════
def create_procmail_inbox():
    """Generate a disposable address via api.procmail.xyz.

    Returns the email address string, or None on failure.
    """
    try:
        resp = requests.get(f"{PROCMAIL_API_BASE}/generate", timeout=15)
        resp.raise_for_status()
        address = resp.text.strip().strip('"')
        if "@" not in address:
            raise RuntimeError(f"unexpected /generate response: {address[:120]!r}")
        print(f"[OK] procmail inbox created: {address}")
        return address
    except Exception as exc:
        print(f"[!] Failed to create procmail inbox: {exc}")
        return None


def _decode_mime_header(value):
    """Best-effort RFC 2047 decode of a MIME-encoded header (Subject/Sender)."""
    if not value:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decode_qp(value):
    """Decode a quoted-printable body if it looks QP-encoded, else return as-is.

    IPTVV's mail (and procmail's PlainTextBody/HTMLBody) is quoted-printable, so
    "Username:=20ABC" / soft line breaks must be decoded before credential regexes
    can match. Plain bodies are left untouched.
    """
    if not value:
        return ""
    if not re.search(r"=[0-9A-Fa-f]{2}|=\r?\n", value):
        return value
    try:
        import quopri
        return quopri.decodestring(value.encode("utf-8", "replace")).decode("utf-8", "replace")
    except Exception:
        return value


def get_procmail_messages(address):
    """Fetch the procmail inbox and normalize it to the shared message shape.

    Each procmail message carries its body inline (PlainTextBody/HTMLBody), so the
    normalized dict already holds 'text'/'html' for extract_credentials_from_email().
    """
    try:
        resp = requests.get(
            f"{PROCMAIL_API_BASE}/inbox/{requests.utils.quote(address)}",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json() or []
    except Exception as exc:
        print(f"[!] Failed to fetch procmail inbox: {exc}")
        return []

    from email.utils import parseaddr

    messages = []
    for idx, item in enumerate(data):
        raw_sender = item.get("Sender", "") or ""
        from_addr = parseaddr(raw_sender)[1] or raw_sender
        text_body = _decode_qp(item.get("PlainTextBody", "") or "")
        html_body = _decode_qp(item.get("HTMLBody") or "")
        messages.append({
            "id": f"{item.get('ReceivedAt', idx)}-{idx}",
            "subject": _decode_mime_header(item.get("Subject", "")),
            "from": {"address": from_addr},
            "text": text_body,
            "html": [html_body] if html_body else [],
        })
    return messages


def _wait_for_credentials_email_procmail(address, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Poll the procmail inbox until the credentials email arrives."""
    print(f"[*] Waiting for credentials email (max {max_wait_seconds}s / {max_wait_seconds//60} minutes)...")
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        print(f"[*] Checking procmail inbox (attempt {attempt}, {remaining}s remaining)...")

        messages = get_procmail_messages(address)
        # Bodies are inline, so fetch_full is the identity function.
        result = _scan_messages_for_credentials(messages, fetch_full=lambda m: m)
        if result:
            return result

        if messages:
            print(f"[*] Found {len(messages)} email(s), but credentials email not yet received")
        else:
            print("[*] Inbox is empty")

        print(f"[*] Waiting {EMAIL_POLL_SECONDS}s before next check...")
        time.sleep(EMAIL_POLL_SECONDS)

    print(f"[!] Timeout: Credentials email not received after {max_wait_seconds}s")
    return None


# ═══════════════════════════════════════════════════════════
# Email backend dispatchers (backend-agnostic entry points used by main())
# ═══════════════════════════════════════════════════════════
def create_email_session(driver=None):
    """Allocate a receiving address using the configured backend (IPTVV_EMAIL_BACKEND).

    Returns a session dict that always carries 'backend' and 'address', or None on
    failure. 'driver' is accepted for API symmetry but unused by the HTTP backends.
    """
    if IPTVV_EMAIL_BACKEND == "mailtm":
        address, password, auth_token = create_mailtm_account()
        if not address:
            return None
        return {"backend": "mailtm", "address": address, "password": password, "token": auth_token}

    # Default: procmail.xyz (8gwifi.org) REST API.
    address = create_procmail_inbox()
    if not address:
        return None
    return {"backend": "procmail", "address": address}


def wait_for_credentials_email(driver, session, max_wait_seconds=EMAIL_MAX_WAIT_SECONDS):
    """Dispatch inbox polling to the configured backend."""
    backend = (session or {}).get("backend")
    if backend == "mailtm":
        return _wait_for_credentials_email_mailtm(session["token"], max_wait_seconds)
    return _wait_for_credentials_email_procmail(session["address"], max_wait_seconds)


# ═══════════════════════════════════════════════════════════
# Selenium Browser Automation
# ═══════════════════════════════════════════════════════════

def get_random_user_agent():
    """Generate a random realistic user agent."""
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    ]
    return random.choice(user_agents)


def get_chrome_major_version(chrome_binary):
    """Return the installed Chrome major version, or None if undetectable."""
    override = os.getenv("CHROME_VERSION_MAIN", "").strip()
    if override.isdigit():
        return int(override)
    if not chrome_binary:
        return None
    try:
        output = subprocess.check_output([chrome_binary, "--version"], text=True, timeout=15)
        match = re.search(r"(\d+)\.", output)
        return int(match.group(1)) if match else None
    except Exception as e:
        print(f"[!] Could not detect Chrome version from {chrome_binary}: {e}")
        return None


def get_driver():
    """Initialize Chrome WebDriver with anti-detection options (undetected-chromedriver).

    Always uses a direct connection on the host's public IP; no proxy.
    """
    headless_mode = os.getenv("HEADLESS", "True").lower() == "true"

    # Use undetected-chromedriver's ChromeOptions
    options = uc.ChromeOptions()

    if headless_mode:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        print("[*] Running in HEADLESS mode")
    else:
        options.add_argument("--start-maximized")
        print("[*] Running in GUI mode")

    # Use random user agent for additional anonymity
    random_ua = get_random_user_agent()
    options.add_argument(f"--user-agent={random_ua}")
    print(f"[*] Using User-Agent: {random_ua[:80]}...")

    # Additional anti-detection options
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-notifications")

    # Additional preferences to appear more human-like
    prefs = {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.images": 1,  # Enable images
    }

    # Always use a direct connection (no proxy). Explicitly disable any proxy
    # to prevent ERR_NO_SUPPORTED_PROXIES from a leaked system/env proxy setting.
    options.add_argument("--no-proxy-server")
    prefs["proxy"] = {
        "mode": "direct",
        "pac_url": "",
        "bypass_list": ""
    }
    print("[*] Using direct connection (public IP, no proxy)")

    options.add_experimental_option("prefs", prefs)

    # Pin the real Chrome binary so undetected-chromedriver never auto-selects a
    # broken snap wrapper (e.g. /usr/lib/chromium-browser, which raises
    # PermissionError on some hosts). CHROME_BINARY overrides; otherwise pick the
    # first installed Google Chrome (in Docker this resolves to google-chrome-stable).
    chrome_binary = os.getenv("CHROME_BINARY", "").strip()
    if not chrome_binary:
        for candidate in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
            if os.path.exists(candidate):
                chrome_binary = candidate
                break
    if chrome_binary:
        options.binary_location = chrome_binary
        print(f"[*] Using Chrome binary: {chrome_binary}")

    # Pin version_main to the installed Chrome; without it undetected-chromedriver
    # downloads the latest driver, which can be one major ahead of the browser
    # bundled in the Apify base image (e.g. driver 150 vs Chrome 149).
    version_main = get_chrome_major_version(chrome_binary)
    if version_main:
        print(f"[*] Detected Chrome major version: {version_main}")

    # Use undetected-chromedriver (no need for chromedriver path, it manages itself)
    try:
        print("[*] Initializing undetected-chromedriver...")
        driver = uc.Chrome(options=options, use_subprocess=False, version_main=version_main)
        print("[OK] undetected-chromedriver initialized successfully")
    except Exception as e:
        print(f"[!] Failed to initialize undetected-chromedriver: {e}")
        print("[*] Falling back to regular ChromeDriver...")
        # Fallback to regular webdriver if undetected fails
        chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/local/bin/chromedriver")
        if os.path.exists(chromedriver_path):
            service = Service(chromedriver_path)
        else:
            service = Service(ChromeDriverManager().install())

        regular_options = Options()

        # Copy all arguments from uc.ChromeOptions to regular Options
        for arg in options.arguments:
            regular_options.add_argument(arg)

        # Copy experimental options (prefs)
        if hasattr(options, 'experimental_options'):
            for key, value in options.experimental_options.items():
                regular_options.add_experimental_option(key, value)

        driver = webdriver.Chrome(service=service, options=regular_options)

    return driver


def safe_click(driver, el):
    """Safely click an element with fallback to JavaScript click."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.4)
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def find_clickable_by_text(driver, terms, timeout=15):
    """Find a clickable element containing any of the given text terms."""
    terms = [term.lower() for term in terms]
    end = time.time() + timeout
    while time.time() < end:
        candidates = driver.find_elements(
            By.XPATH,
            "//a|//button|//input[@type='submit' or @type='button']|//label|//*[@role='button']",
        )
        for el in candidates:
            if not el.is_displayed() or not el.is_enabled():
                continue
            text = " ".join(
                filter(
                    None,
                    [
                        el.text,
                        el.get_attribute("value"),
                        el.get_attribute("title"),
                        el.get_attribute("aria-label"),
                    ],
                )
            ).lower()
            if any(term in text for term in terms):
                return el
        time.sleep(0.5)
    raise TimeoutError(f"Could not find clickable element containing: {terms}")


def page_text_lower(driver):
    """Return visible body text, lowercased, without failing page checks."""
    try:
        return driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        return ""


def is_cloudflare_block_page(driver):
    """Detect Cloudflare/WAF pages that replace the real WooCommerce checkout."""
    title = (driver.title or "").lower()
    current_url = (driver.current_url or "").lower()
    body = page_text_lower(driver)
    markers = [
        "attention required",
        "cloudflare",
        "checking your browser",
        "verify you are human",
        "cf-browser-verification",
        "ray id",
        "error 1020",
        "access denied",
    ]
    return (
        "cloudflare" in title
        or "cdn-cgi" in current_url
        or any(marker in body for marker in markers)
    )


def is_browser_error_page(driver):
    """Detect Chrome's own network-error screen (NOT a Cloudflare block).

    Chrome renders these (e.g. ERR_NO_SUPPORTED_PROXIES, DNS failures, timeouts)
    when it never reaches the site at all, so they must not be mistaken for a
    Cloudflare/WAF block or a missing checkout form.
    """
    body = page_text_lower(driver)
    current_url = (driver.current_url or "").lower()
    markers = [
        "this site can't be reached",
        "this site can’t be reached",
        "err_",
        "dns_probe_finished",
        "took too long to respond",
        "your internet access is blocked",
        "no internet",
    ]
    return current_url.startswith("chrome-error://") or any(marker in body for marker in markers)


def save_page_debug_artifacts(driver, label):
    """Save a screenshot and HTML snapshot for production diagnosis."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or "page"
    debug_dir = IPTVV_DEBUG_DIR
    artifacts = {}

    # Try to create/access the debug directory, fallback to user-writable locations
    fallback_dirs = [debug_dir, "./logs", os.path.expanduser("~/logs"), "/tmp"]
    debug_dir_accessible = False

    for candidate_dir in fallback_dirs:
        try:
            os.makedirs(candidate_dir, exist_ok=True)
            # Test write access by creating a test file
            test_file = os.path.join(candidate_dir, f".write_test_{os.getpid()}")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            debug_dir = candidate_dir
            debug_dir_accessible = True
            if candidate_dir != IPTVV_DEBUG_DIR:
                print(f"[*] Using fallback debug directory: {debug_dir}")
            break
        except Exception as exc:
            if candidate_dir == fallback_dirs[-1]:
                print(f"[!] Could not access any debug directory, last error: {exc}")
            continue

    base_path = os.path.join(debug_dir, f"iptvv_{safe_label}_{timestamp}")

    try:
        screenshot_path = f"{base_path}.png"
        driver.save_screenshot(screenshot_path)
        print(f"[*] Screenshot saved to: {screenshot_path}")
        artifacts["screenshot"] = screenshot_path
    except Exception as exc:
        print(f"[!] Could not save screenshot: {exc}")

    try:
        html_path = f"{base_path}.html"
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)
        print(f"[*] HTML snapshot saved to: {html_path}")
        artifacts["html"] = html_path
    except Exception as exc:
        print(f"[!] Could not save HTML snapshot: {exc}")

    return artifacts


def get_public_ip():
    """Best-effort public IP lookup for Cloudflare allowlist/support tickets."""
    try:
        response = requests.get("https://api.ipify.org", timeout=8)
        response.raise_for_status()
        return response.text.strip()
    except Exception as exc:
        print(f"[!] Could not fetch public IP: {exc}")
        return "unknown"


def extract_cloudflare_diagnostics(driver):
    """Extract useful Cloudflare details from the block page."""
    source = driver.page_source or ""
    page_text = html.unescape(re.sub(r"<[^>]+>", " ", source))
    page_text = re.sub(r"\s+", " ", page_text).strip()

    ray_id = "unknown"
    ray_match = re.search(r"Ray ID:?\s*([0-9a-fA-F]{12,})", page_text)
    if ray_match:
        ray_id = ray_match.group(1)

    reason = "Cloudflare/WAF block page"
    reason_patterns = [
        r"You are unable to access[^.]*\.?",
        r"Sorry, you have been blocked\.?",
        r"Attention Required![^.]*\.?",
        r"Access denied\.?",
    ]
    for pattern in reason_patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            reason = match.group(0).strip()
            break

    return {
        "ray_id": ray_id,
        "reason": reason,
        "url": driver.current_url,
        "title": driver.title,
    }


def get_browser_public_ip(driver):
    """Fetch public IP from inside Chrome to verify VPN/browser egress."""
    original_url = driver.current_url
    try:
        driver.get(IPTVV_PROXY_CHECK_URL)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        browser_ip = driver.find_element(By.TAG_NAME, "body").text.strip()
        browser_ip = re.sub(r"\s+", " ", browser_ip)
        if browser_ip:
            return browser_ip
        return "unknown"
    except Exception as exc:
        print(f"[!] Could not fetch browser public IP from {IPTVV_PROXY_CHECK_URL}: {exc}")
        return "unknown"
    finally:
        if original_url and original_url != "data:,":
            try:
                driver.get(original_url)
            except Exception:
                pass


def preflight_checkout_access():
    """Check whether the current prod egress can reach the real IPTVV checkout."""
    driver = None
    checkout_url = f"{IPTVV_BASE_URL}/checkout/"

    print("\n" + "=" * 60)
    print("IPTVV CANADA - CHECKOUT PREFLIGHT")
    print("=" * 60)
    print(f"[*] Checkout URL: {checkout_url}")
    print(f"[*] Debug directory: {IPTVV_DEBUG_DIR}")
    print("=" * 60 + "\n")

    try:
        server_ip = get_public_ip()
        print(f"[*] Server public IP: {server_ip}")

        driver = get_driver()
        browser_ip = get_browser_public_ip(driver)
        print(f"[*] Browser-visible public IP: {browser_ip}")
        if IPTVV_KNOWN_BLOCKED_IP and browser_ip == IPTVV_KNOWN_BLOCKED_IP:
            print(f"[!] Browser is still using known blocked IPTVV IP: {IPTVV_KNOWN_BLOCKED_IP}")
        if server_ip != "unknown" and browser_ip != "unknown" and server_ip == browser_ip:
            print("[*] Browser IP matches server IP; this is expected for full-system VPN egress.")

        print("[*] Seeding free-trial cart for checkout preflight...")
        driver.get(f"{IPTVV_BASE_URL}/?add-to-cart={IPTVV_TRIAL_PRODUCT_ID}")
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(5)

        print(f"[*] Opening checkout: {checkout_url}")
        driver.get(checkout_url)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(5)

        artifacts = save_page_debug_artifacts(driver, "preflight_checkout")
        print(f"[*] Checkout URL after load: {driver.current_url}")
        print(f"[*] Page title: {driver.title}")

        if is_cloudflare_block_page(driver):
            diagnostics = extract_cloudflare_diagnostics(driver)
            print(f"[!] Cloudflare Ray ID: {diagnostics['ray_id']}")
            print(f"[!] Cloudflare reason: {diagnostics['reason']}")
            raise CloudflareBlockedError(
                "Preflight failed: IPTVV checkout is blocked by Cloudflare/WAF. "
                f"Server IP: {server_ip}; browser IP: {browser_ip}; "
                f"Cloudflare Ray ID: {diagnostics['ray_id']}; "
                f"debug HTML: {artifacts.get('html', 'not saved')}; "
                f"screenshot: {artifacts.get('screenshot', 'not saved')}."
            )

        if is_browser_error_page(driver):
            print("[!] Chrome rendered a network-error page (NOT a Cloudflare block)")
            raise RuntimeError(
                "Preflight failed: network/connectivity error - Chrome never reached IPTVV "
                "(this is NOT a Cloudflare IP block; check DNS/egress/leftover proxy settings). "
                f"Server IP: {server_ip}; browser IP: {browser_ip}; "
                f"Current URL: {driver.current_url}; title: {driver.title}; "
                f"debug HTML: {artifacts.get('html', 'not saved')}; "
                f"screenshot: {artifacts.get('screenshot', 'not saved')}."
            )

        try:
            driver.find_element(By.ID, "billing_email")
        except Exception:
            raise RuntimeError(
                "Preflight failed: no Cloudflare block and no network error detected, "
                "but the checkout billing_email field is missing (unexpected page layout). "
                f"Server IP: {server_ip}; browser IP: {browser_ip}; "
                f"Current URL: {driver.current_url}; title: {driver.title}; "
                f"debug HTML: {artifacts.get('html', 'not saved')}; "
                f"screenshot: {artifacts.get('screenshot', 'not saved')}."
            )

        print("[OK] Preflight passed: IPTVV checkout form is reachable.")
        print(f"[OK] Server IP: {server_ip}")
        print(f"[OK] Browser-visible IP: {browser_ip}")
        print(f"[OK] Debug HTML: {artifacts.get('html', 'not saved')}")
        print(f"[OK] Screenshot: {artifacts.get('screenshot', 'not saved')}")
        return True

    finally:
        if driver:
            try:
                driver.quit()
                print("[*] Browser closed")
            except Exception:
                pass


def wait_for_real_checkout_page(driver, context, timeout=30):
    """Wait until the WooCommerce checkout appears, or fail on a blocker page."""
    print(f"[*] Verifying checkout page after {context}...")
    deadline = time.time() + timeout
    cloudflare_seen = False
    reload_attempts = 0

    while time.time() < deadline:
        if is_cloudflare_block_page(driver):
            cloudflare_seen = True
            print("[!] Cloudflare/WAF page detected instead of checkout; waiting for clearance...")
            if reload_attempts < IPTVV_PAGE_LOAD_RETRIES:
                reload_attempts += 1
                print(f"[*] Reloading page after blocker detection ({reload_attempts}/{IPTVV_PAGE_LOAD_RETRIES})...")
                driver.refresh()
            time.sleep(5)
            continue

        try:
            driver.find_element(By.ID, "billing_email")
            print("[OK] Billing email field detected - checkout form is loaded")
            return True
        except Exception:
            time.sleep(1)

    if cloudflare_seen or is_cloudflare_block_page(driver):
        artifacts = save_page_debug_artifacts(driver, "cloudflare_block")
        diagnostics = extract_cloudflare_diagnostics(driver)
        public_ip = get_public_ip()
        print(f"[!] Cloudflare Ray ID: {diagnostics['ray_id']}")
        print(f"[!] Cloudflare reason: {diagnostics['reason']}")
        print(f"[!] Production public IP: {public_ip}")
        raise CloudflareBlockedError(
            "IPTVV checkout is blocked by Cloudflare/WAF in this production container. "
            "The checkout form never loaded. "
            f"Public IP: {public_ip}; Cloudflare Ray ID: {diagnostics['ray_id']}; "
            f"debug HTML: {artifacts.get('html', 'not saved')}; "
            f"screenshot: {artifacts.get('screenshot', 'not saved')}. "
            "Ask IPTVV/Cloudflare to allowlist this server or provide an approved API/integration path."
        )

    artifacts = save_page_debug_artifacts(driver, "checkout_not_loaded")
    empty_cart_hint = (
        " (page shows an empty cart - the trial product was never added)"
        if "your cart is currently empty" in page_text_lower(driver)
        else ""
    )
    raise RuntimeError(
        f"IPTVV checkout form did not load after {context}{empty_cart_hint}. "
        f"Current URL: {driver.current_url}; title: {driver.title}; "
        f"debug HTML: {artifacts.get('html', 'not saved')}; "
        f"screenshot: {artifacts.get('screenshot', 'not saved')}"
    )


def generate_random_user_data():
    """Generate random user data for checkout form."""
    first_names = ["John", "Jane", "Mike", "Sarah", "David", "Emma", "Chris", "Lisa", "Tom", "Amy"]
    last_names = ["Smith", "Johnson", "Brown", "Davis", "Wilson", "Moore", "Taylor", "Anderson"]

    first = random.choice(first_names)
    last = random.choice(last_names)

    # Generate valid Canadian phone number (area codes: 416, 514, 604, 403, 613)
    area_codes = ["416", "514", "604", "403", "613", "647", "438", "778", "587", "343"]
    area_code = random.choice(area_codes)
    exchange = f"{random.randint(200, 999)}"  # Central office code
    line = f"{random.randint(1000, 9999)}"    # Line number
    phone = f"({area_code}) {exchange}-{line}"  # Format: (416) 555-1234

    return {
        "first_name": first,
        "last_name": last,
        "phone": phone,
        "address": f"{random.randint(100, 9999)} {random.choice(['Main', 'Oak', 'Maple', 'Cedar'])} St",
        "city": random.choice(["Toronto", "Montreal", "Vancouver", "Calgary", "Ottawa"]),
        "postal_code": f"{random.choice('ABCEGHJKLMNPRSTVXY')}{random.randint(0, 9)}{random.choice('ABCEGHJKLMNPRSTVWXYZ')} {random.randint(0, 9)}{random.choice('ABCEGHJKLMNPRSTVWXYZ')}{random.randint(0, 9)}",
        "country": "Canada",
    }


# ═══════════════════════════════════════════════════════════
# IPTVV.ca Automation Functions
# ═══════════════════════════════════════════════════════════

def find_add_to_cart_control(driver, timeout=10):
    """Find the control that actually adds the trial product to the cart.

    Prefer anchors whose href performs the WooCommerce add-to-cart (the
    "START MY FREE TRIAL" button); matching by label alone can hit the
    "Get Free Trial" link, which only opens the /iptv-free-trial/ landing
    page without touching the cart.
    """
    end = time.time() + timeout
    while time.time() < end:
        for el in driver.find_elements(By.CSS_SELECTOR, "a[href*='add-to-cart']"):
            if el.is_displayed() and el.is_enabled():
                return el
        time.sleep(0.5)
    return find_clickable_by_text(
        driver,
        ["start my free trial", "start free trial", "add to cart"],
        timeout=10,
    )


def cart_has_items(driver):
    """Load the cart page and report whether it contains at least one item."""
    driver.get(IPTVV_CART_URL)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(2)
    if driver.find_elements(By.CSS_SELECTOR, ".cart_item"):
        return True
    if "your cart is currently empty" in page_text_lower(driver):
        return False
    # Unknown layout: proceed and let the checkout verification decide.
    return True


def open_checkout_and_verify(driver, context):
    """Navigate to the checkout page and wait for the real WooCommerce form."""
    checkout_url = f"{IPTVV_BASE_URL}/checkout/"
    print(f"[*] Navigating to checkout: {checkout_url}")
    driver.get(checkout_url)
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(5)

    # Wait for document to be fully ready
    for _ in range(10):
        if driver.execute_script("return document.readyState") == "complete":
            break
        time.sleep(1)

    print(f"[*] Checkout URL: {driver.current_url}")
    print(f"[*] Page title: {driver.title}")
    wait_for_real_checkout_page(driver, context, timeout=IPTVV_CLOUDFLARE_WAIT_SECONDS)


def navigate_to_cart_and_get_free_trial(driver):
    """Add the free-trial product to the cart and open checkout (WooCommerce flow)."""
    print(f"[*] Navigating to IPTVV cart: {IPTVV_CART_URL}")
    driver.get(IPTVV_CART_URL)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(2)
    print(f"[*] Current URL: {driver.current_url}")

    print("[*] Looking for the add-to-cart trial button...")
    try:
        trial_button = find_add_to_cart_control(driver, timeout=10)
        label = trial_button.text or trial_button.get_attribute("href")
        print(f"[OK] Found element: {label}")
        safe_click(driver, trial_button)
        print("[OK] Clicked trial button - adding product to cart...")
        time.sleep(5)
        print(f"[*] After click URL: {driver.current_url}")
    except TimeoutError:
        print("[!] Add-to-cart trial button not found")

    if not cart_has_items(driver):
        print("[*] Cart is still empty; trying direct add-to-cart URL...")
        driver.get(f"{IPTVV_BASE_URL}/?add-to-cart={IPTVV_TRIAL_PRODUCT_ID}")
        time.sleep(8)  # Wait longer for product to be added
        print(f"[*] After add-to-cart URL: {driver.current_url}")
        if not cart_has_items(driver):
            print("[!] Cart still looks empty after direct add-to-cart; continuing to checkout anyway")

    try:
        open_checkout_and_verify(driver, "free-trial cart flow")
    except RuntimeError:
        if "/cart" not in driver.current_url:
            raise
        print("[!] Checkout bounced back to the cart; re-adding trial product and retrying once...")
        driver.get(f"{IPTVV_BASE_URL}/?add-to-cart={IPTVV_TRIAL_PRODUCT_ID}")
        time.sleep(8)
        open_checkout_and_verify(driver, "direct add-to-cart retry")


def select_full_channel_package(driver):
    """Select 'full channel' option from dropdown or radio buttons."""
    print("[*] Looking for 'full channel' option...")

    # Strategy 1: Check dropdowns/select elements
    for select_el in driver.find_elements(By.TAG_NAME, "select"):
        select = Select(select_el)
        options_text = " ".join([opt.text.lower() for opt in select.options])

        if "channel" in options_text or "package" in options_text or "plan" in options_text:
            print(f"[*] Found dropdown with channel options")
            for option in select.options:
                if "full" in option.text.lower() and "channel" in option.text.lower():
                    select.select_by_visible_text(option.text)
                    print(f"[OK] Selected: {option.text}")
                    return True
            # If no exact match, select the most comprehensive option (usually last or contains 'all')
            for option in select.options:
                if "all" in option.text.lower() or "full" in option.text.lower():
                    select.select_by_visible_text(option.text)
                    print(f"[OK] Selected: {option.text}")
                    return True

    # Strategy 2: Check radio buttons
    radio_buttons = driver.find_elements(By.XPATH, "//input[@type='radio']")
    for radio in radio_buttons:
        label_text = ""
        try:
            # Try to find associated label
            radio_id = radio.get_attribute("id")
            if radio_id:
                label = driver.find_element(By.XPATH, f"//label[@for='{radio_id}']")
                label_text = label.text.lower()
        except:
            # Try parent element text
            label_text = radio.find_element(By.XPATH, "./parent::*").text.lower()

        if "full" in label_text and "channel" in label_text:
            safe_click(driver, radio)
            print(f"[OK] Selected radio button: {label_text}")
            return True

    # Strategy 3: Check checkboxes
    checkboxes = driver.find_elements(By.XPATH, "//input[@type='checkbox']")
    for checkbox in checkboxes:
        label_text = ""
        try:
            checkbox_id = checkbox.get_attribute("id")
            if checkbox_id:
                label = driver.find_element(By.XPATH, f"//label[@for='{checkbox_id}']")
                label_text = label.text.lower()
        except:
            label_text = checkbox.find_element(By.XPATH, "./parent::*").text.lower()

        if "full" in label_text and "channel" in label_text:
            if not checkbox.is_selected():
                safe_click(driver, checkbox)
            print(f"[OK] Checked: {label_text}")
            return True

    print("[*] Could not find 'full channel' option - may not be required or already selected")
    return False


def fill_checkout_form(driver, email_address):
    """Fill WooCommerce checkout form with generated data and mail.tm email."""
    print("[*] Filling WooCommerce checkout form...")

    wait_for_real_checkout_page(driver, "form fill step", timeout=IPTVV_CLOUDFLARE_WAIT_SECONDS)
    time.sleep(2)  # Extra time for all form fields to render

    user_data = generate_random_user_data()
    user_data["email"] = email_address

    # WooCommerce standard billing field names
    field_mappings = {
        "email": ["billing_email", "email"],
        "first_name": ["billing_first_name", "firstname", "first_name"],
        "last_name": ["billing_last_name", "lastname", "last_name"],
        "phone": ["billing_phone", "phone"],
        "address": ["billing_address_1", "address", "address1"],
        "city": ["billing_city", "city"],
        "postal_code": ["billing_postcode", "postal", "postcode", "zip"],
        "country": ["billing_country", "country"],
    }

    # Try to fill each field
    filled_fields = []
    for data_key, field_names in field_mappings.items():
        value = user_data.get(data_key, "")
        if not value:
            continue

        filled = False
        for field_name in field_names:
            # Try by ID first (WooCommerce uses IDs), then name
            for by_type in [By.ID, By.NAME]:
                try:
                    field = driver.find_element(by_type, field_name)
                    if field.is_displayed() and field.is_enabled():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", field)
                        time.sleep(0.3)
                        field.clear()
                        field.send_keys(value)
                        print(f"[OK] Filled {field_name}: {value if data_key != 'email' else email_address}")
                        filled = True
                        filled_fields.append(field_name)
                        break
                except:
                    pass
            if filled:
                break

        if not filled:
            print(f"[!] Could not find field for: {data_key}")

    # Handle country dropdown (WooCommerce uses select2 sometimes)
    try:
        # Try standard select
        country_select = driver.find_element(By.ID, "billing_country")
        select = Select(country_select)
        for option in select.options:
            if "ca" == option.get_attribute("value").lower() or "canada" in option.text.lower():
                select.select_by_value(option.get_attribute("value"))
                print(f"[OK] Selected country: Canada")
                filled_fields.append("billing_country")
                break
    except:
        # Try by name if ID doesn't work
        try:
            country_select = driver.find_element(By.NAME, "billing_country")
            select = Select(country_select)
            select.select_by_value("CA")
            print(f"[OK] Selected country: CA")
            filled_fields.append("billing_country")
        except:
            print("[!] Could not find country dropdown")

    # WooCommerce often has a state/province field
    try:
        state_select = driver.find_element(By.ID, "billing_state")
        select = Select(state_select)
        # Select Ontario (ON) as default
        select.select_by_value("ON")
        print(f"[OK] Selected state: Ontario")
        filled_fields.append("billing_state")
    except:
        pass

    # IPTVV.ca CUSTOM REQUIRED FIELDS
    # Handle "Device Select" checkboxes (required by IPTVV.ca)
    print("[*] Looking for device selection checkboxes...")
    device_filled = False
    try:
        # Find device checkboxes by name="device_select[]"
        device_checkboxes = driver.find_elements(By.NAME, "device_select[]")
        if device_checkboxes:
            print(f"[*] Found {len(device_checkboxes)} device checkboxes")
            # Check the first device checkbox (Android TV Box or Firestick)
            for checkbox in device_checkboxes:
                value = checkbox.get_attribute("value") or ""
                # Prefer Android Box or Smart TV
                if value in ["androidbox", "smarttv", "firetv"]:
                    if not checkbox.is_selected():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox)
                        time.sleep(0.3)
                        safe_click(driver, checkbox)
                        print(f"[OK] Selected device: {value}")
                        filled_fields.append("device_select")
                        device_filled = True
                        break

            # Fallback: check first device if none selected
            if not device_filled and len(device_checkboxes) > 0:
                checkbox = device_checkboxes[0]
                if not checkbox.is_selected():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox)
                    time.sleep(0.3)
                    safe_click(driver, checkbox)
                    value = checkbox.get_attribute("value") or "first"
                    print(f"[OK] Selected device (first): {value}")
                    filled_fields.append("device_select")
                    device_filled = True
        else:
            print("[!] No device checkboxes found with name='device_select[]'")
    except Exception as e:
        print(f"[!] Device checkbox error: {e}")

    # Handle "Billing Channel Packages" field (required by IPTVV.ca)
    # This might be a multiselect, checkbox group, or hidden field
    try:
        # Strategy 1: Try to find checkboxes or radio buttons for packages
        package_checkboxes = driver.find_elements(By.XPATH, "//input[@type='checkbox' and (contains(@id, 'channel') or contains(@name, 'channel') or contains(@id, 'package') or contains(@name, 'package'))]")
        if package_checkboxes:
            # Check all packages (or first one for "full channel")
            for checkbox in package_checkboxes[:1]:  # Select first/main package
                if not checkbox.is_selected():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox)
                    time.sleep(0.3)
                    safe_click(driver, checkbox)
                    print(f"[OK] Selected channel package checkbox")
                    filled_fields.append("billing_channel_packages")
                    break

        # Strategy 2: Try to find a select dropdown for packages
        else:
            for package_id in ["billing_channel_packages", "billing_packages", "channel_packages", "packages"]:
                try:
                    package_field = driver.find_element(By.ID, package_id)
                    if package_field.tag_name == "select":
                        select = Select(package_field)
                        # Select option containing "full" or "all" or just first option
                        selected = False
                        for option in select.options:
                            if "full" in option.text.lower() or "all" in option.text.lower():
                                select.select_by_visible_text(option.text)
                                print(f"[OK] Selected package: {option.text}")
                                filled_fields.append("billing_channel_packages")
                                selected = True
                                break
                        if not selected and len(select.options) > 1:
                            select.select_by_index(1)
                            print(f"[OK] Selected package (first option): {select.options[1].text}")
                            filled_fields.append("billing_channel_packages")
                        break
                except:
                    continue
    except Exception as e:
        print(f"[!] Could not find or fill channel packages field: {e}")

    print(f"[OK] Filled {len(filled_fields)} fields: {', '.join(filled_fields)}")
    print(f"[*] Using email: {email_address}")
    return user_data


def solve_recaptcha_v2(driver, timeout=120, max_retries=2):
    """Solve reCAPTCHA v2 using 2captcha service."""
    if not solver:
        print("[*] 2captcha solver not configured; checking if CAPTCHA exists...")
        has_captcha = any("recaptcha" in (iframe.get_attribute("src") or "")
                         for iframe in driver.find_elements(By.TAG_NAME, "iframe"))
        if has_captcha:
            print("[!] reCAPTCHA detected but TWOCAPTCHA_API_KEY not set")
            if os.getenv("HEADLESS", "True").lower() != "true":
                input("[*] Please solve the CAPTCHA manually in the browser, then press ENTER here...")
                return True
            return False
        return True

    site_key = None
    for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
        src = iframe.get_attribute("src") or ""
        if "recaptcha" in src and "api2/anchor" in src and "k=" in src:
            site_key = src.split("k=", 1)[1].split("&", 1)[0]
            break

    if not site_key:
        print("[*] No reCAPTCHA iframe found")
        return True

    print(f"[*] Solving reCAPTCHA with site key: {site_key}")
    for attempt in range(1, max_retries + 1):
        try:
            result = solver.recaptcha(sitekey=site_key, url=driver.current_url, version="v2", invisible=0)
            token = result["code"]
            driver.execute_script(
                """
                var textarea = document.getElementById('g-recaptcha-response');
                if (textarea) {
                    textarea.value = arguments[0];
                    textarea.innerHTML = arguments[0];
                    textarea.dispatchEvent(new Event('change', {bubbles:true}));
                }
                var el = document.querySelector('.g-recaptcha');
                if (el) {
                    var callback = el.getAttribute('data-callback');
                    if (callback && typeof window[callback] === 'function') {
                        window[callback](arguments[0]);
                    }
                }
                """,
                token,
            )
            print("[OK] reCAPTCHA token injected")
            time.sleep(2)
            return True
        except Exception as exc:
            print(f"[!] reCAPTCHA attempt {attempt}/{max_retries} failed: {exc}")
            if attempt < max_retries:
                time.sleep(5)
    return False


def submit_checkout_form(driver):
    """Submit the WooCommerce checkout form."""
    print("[*] Submitting WooCommerce checkout form...")
    wait_for_real_checkout_page(driver, "submit step", timeout=IPTVV_CLOUDFLARE_WAIT_SECONDS)

    # Solve CAPTCHA if present
    if not solve_recaptcha_v2(driver):
        raise RuntimeError("Failed to solve reCAPTCHA")

    # WooCommerce usually has a button with ID "place_order"
    submit_btn = None
    try:
        # Try WooCommerce standard button ID
        submit_btn = driver.find_element(By.ID, "place_order")
        print(f"[OK] Found WooCommerce place_order button")
    except:
        # Fallback to text search
        try:
            submit_btn = find_clickable_by_text(
                driver,
                ["place order", "place trial order", "submit", "checkout", "complete order"],
                timeout=15
            )
            print(f"[OK] Found submit button: {submit_btn.text}")
        except TimeoutError:
            print("[!] Could not find submit button")

    if submit_btn:
        # Scroll to button
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
        time.sleep(1)

        # Click submit
        safe_click(driver, submit_btn)
        print("[OK] Submit button clicked")

        # Wait for processing/redirect (WooCommerce shows processing animation)
        print("[*] Waiting for order processing...")
        time.sleep(10)  # WooCommerce needs more time to process
        print(f"[*] After submit URL: {driver.current_url}")

        # Check if we got redirected to thank you / order received page
        if "order-received" in driver.current_url or "thank-you" in driver.current_url:
            print("[OK] Order successfully submitted! (on thank you page)")
        elif "checkout" in driver.current_url:
            # Still on checkout - might be validation errors
            print("[!] WARNING: Still on checkout page - order submission failed")

            # Save screenshot for debugging
            try:
                screenshot_path = "/tmp/iptvv_checkout_error.png"
                driver.save_screenshot(screenshot_path)
                print(f"[*] Screenshot saved to: {screenshot_path}")
            except:
                pass

            # Try to find error messages
            try:
                error_msg = driver.find_element(By.CSS_SELECTOR, ".woocommerce-error, .woocommerce-NoticeGroup-checkout, .woocommerce-notices-wrapper")
                print(f"[!] Checkout validation error: {error_msg.text}")
                # Print full page errors for debugging
                all_errors = driver.find_elements(By.CSS_SELECTOR, ".woocommerce-error li, .woocommerce-NoticeGroup-checkout li")
                if all_errors:
                    for i, err in enumerate(all_errors, 1):
                        print(f"    Error {i}: {err.text}")
            except:
                print("[*] No error message found via CSS selectors")

            # Check for inline field errors
            try:
                field_errors = driver.find_elements(By.CSS_SELECTOR, ".woocommerce-invalid-required-field, [aria-invalid='true']")
                if field_errors:
                    print(f"[!] Found {len(field_errors)} invalid/required fields:")
                    for field in field_errors:
                        field_name = field.get_attribute("name") or field.get_attribute("id") or "unknown"
                        print(f"    - {field_name}")
            except:
                pass

            # Print page source snippet around errors
            try:
                page_text = driver.find_element(By.TAG_NAME, "body").text
                if "error" in page_text.lower():
                    print("[*] Page contains 'error' keyword - checking...")
                    lines = page_text.split("\n")
                    for i, line in enumerate(lines):
                        if "error" in line.lower() or "required" in line.lower() or "invalid" in line.lower():
                            context_start = max(0, i-1)
                            context_end = min(len(lines), i+2)
                            print(f"[!] Error context: {' '.join(lines[context_start:context_end])}")
                            break
            except:
                pass
    else:
        raise RuntimeError("Could not find or click submit button")


# ═══════════════════════════════════════════════════════════
# Credential Extraction
# ═══════════════════════════════════════════════════════════

def extract_credentials_from_email(message):
    """
    Extract username, password, and hostname from mail.tm message.

    Args:
        message: Full message object from mail.tm API with 'text' and 'html' fields

    Returns:
        tuple: (username, password, hostname) or (None, None, None) if extraction fails
    """
    # Get both text and HTML versions
    text_content = message.get("text", "")
    html_content = message.get("html", [])

    # Combine for comprehensive search
    if isinstance(html_content, list):
        html_content = " ".join(html_content)

    combined_content = f"{text_content}\n\n{html_content}"

    # Unescape HTML entities
    for _ in range(3):
        unescaped = html.unescape(combined_content)
        if unescaped == combined_content:
            break
        combined_content = unescaped

    # Clean HTML tags
    normalized = re.sub(r"<br\s*/?>", "\n", combined_content, flags=re.I)
    normalized = re.sub(r"</p\s*>", "\n", normalized, flags=re.I)
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)

    print("[*] Extracting credentials from email...")
    print("[*] Email preview:")
    print(normalized[:500])

    # Extraction patterns based on the example email format
    username = None
    password = None
    hostname = None

    # Username patterns
    username_patterns = [
        r"Username\s*:?\s*([A-Z0-9]{10,})",  # GABSSZY5RS format
        r"Username\s*:?\s*([^\s\n<]+)",
        r"User\s*:?\s*([^\s\n<]+)",
    ]

    for pattern in username_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            username = match.group(1).strip()
            print(f"[*] Found username: {username}")
            break

    # Password patterns
    password_patterns = [
        r"Password\s*:?\s*(\d{8,})",  # 49180341 format
        r"Password\s*:?\s*([^\s\n<]+)",
        r"Pass\s*:?\s*([^\s\n<]+)",
    ]

    for pattern in password_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            password = match.group(1).strip()
            print(f"[*] Found password: {password}")
            break

    # Hostname/Server Address patterns
    hostname_patterns = [
        r"Server Address[^:]*:?\s*(https?://[^\s<>'\"]+)",
        r"Playlist Host[^:]*:?\s*(https?://[^\s<>'\"]+)",
        r"Host[^:]*:?\s*(https?://[^\s<>'\"]+)",
        r"Server[^:]*:?\s*(https?://[^\s<>'\"]+)",
    ]

    for pattern in hostname_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            hostname = match.group(1).strip().rstrip(".,)")
            print(f"[*] Found hostname: {hostname}")
            break

    # Fallback: find any URL that's not iptvv.ca or common services
    if not hostname:
        urls = re.findall(r"https?://[^\s<>'\"]+", normalized)
        for url in urls:
            lowered = url.lower()
            if "iptvv.ca" not in lowered and "mail.tm" not in lowered:
                hostname = url.rstrip(".,)")
                print(f"[*] Found hostname (fallback): {hostname}")
                break

    return username, password, hostname


def save_to_iboplayer(username, password, hostname, max_retries=3):
    """
    Save IPTVV playlist to IBO Player using their API.

    Args:
        username: IPTVV username
        password: IPTVV password
        hostname: IPTVV server hostname/URL
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        bool: True if successful, False otherwise
    """
    if not IPTVV_IBOPLAYER_ENABLED:
        print("[*] IBO Player integration is disabled (IPTVV_IBOPLAYER_ENABLED=False)")
        return False

    if not IPTVV_IBOPLAYER_MAC_ADDRESS or not IPTVV_IBOPLAYER_DEVICE_KEY \
            or not TWOCAPTCHA_API_KEY or not IPTVV_IBOPLAYER_PLAYLIST_URL_ID:
        print("[!] IBO Player integration enabled but missing required credentials:")
        print(f"    - IPTVV_IBOPLAYER_MAC_ADDRESS: {'Set' if IPTVV_IBOPLAYER_MAC_ADDRESS else 'Missing'}")
        print(f"    - IPTVV_IBOPLAYER_DEVICE_KEY: {'Set' if IPTVV_IBOPLAYER_DEVICE_KEY else 'Missing'}")
        print(f"    - TWOCAPTCHA_API_KEY: {'Set' if TWOCAPTCHA_API_KEY else 'Missing'}")
        print(f"    - IPTVV_IBOPLAYER_PLAYLIST_URL_ID: {'Set' if IPTVV_IBOPLAYER_PLAYLIST_URL_ID else 'Missing'}")
        return False

    api_url = "https://iboplayer.com/frontend/device/savePlaylist"

    # Authenticate with a device-login bearer token (obtained/cached by
    # iboplayer_auth). A 401/403 below triggers a token refresh + one retry.
    try:
        headers = iboplayer_auth.authed_headers(
            IPTVV_IBOPLAYER_MAC_ADDRESS, IPTVV_IBOPLAYER_DEVICE_KEY
        )
    except Exception as e:
        print(f"[!] Could not obtain IBO Player bearer token: {e}")
        return False

    # Construct the playlist URL using the hostname from credentials
    playlist_url = hostname.rstrip("/")

    payload = {
        "current_playlist_url_id": IPTVV_IBOPLAYER_PLAYLIST_URL_ID,
        "password": password,
        "pin": "",
        "playlist_name": IPTVV_IBOPLAYER_PLAYLIST_NAME,
        "playlist_type": "xc",  # Xtream Codes format
        "playlist_url": playlist_url,
        "protect": "false",
        "username": username,
        "xml_url": ""
    }

    print("\n" + "=" * 60)
    print("[*] Saving playlist to IBO Player...")
    print("=" * 60)
    print(f"[*] API URL: {api_url}")
    print(f"[*] Playlist Name: {IPTVV_IBOPLAYER_PLAYLIST_NAME}")
    print(f"[*] Playlist URL: {playlist_url}")
    print(f"[*] Username: {username}")
    print(f"[*] Password: {password}")
    print("=" * 60)

    relogin_attempted = False
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                # HTTP 200 alone is NOT success: IBO Player returns
                # {"status":"error"} in the body when it rejects the save
                # (e.g. the current_playlist_url_id isn't owned by this device).
                try:
                    response_data = response.json()
                except Exception:
                    response_data = None
                print(f"[*] IBO Player response: {response_data if response_data is not None else response.text[:300]}")

                if isinstance(response_data, dict) and response_data.get("status") == "success":
                    print(f"[OK] Playlist saved to IBO Player successfully!")
                    return True

                print("[!] IBO Player rejected the save (status != success).")
                print("[!] Most common cause: IPTVV_IBOPLAYER_PLAYLIST_URL_ID is not a "
                      "playlist owned by the logged-in device "
                      f"({IPTVV_IBOPLAYER_MAC_ADDRESS}). Verify the device MAC/key and "
                      "playlist URL ID match the same IBO Player device.")
                return False

            elif response.status_code in (401, 403):
                # Expired/invalid bearer token - refresh it once and retry.
                print(f"[!] IBO Player auth rejected ({response.status_code}): {response.text[:200]}")
                if not relogin_attempted:
                    relogin_attempted = True
                    print("[*] Refreshing bearer token and retrying...")
                    iboplayer_auth.clear_cached_token(IPTVV_IBOPLAYER_MAC_ADDRESS)
                    try:
                        headers = iboplayer_auth.authed_headers(
                            IPTVV_IBOPLAYER_MAC_ADDRESS, IPTVV_IBOPLAYER_DEVICE_KEY,
                            force_refresh=True,
                        )
                    except Exception as e:
                        print(f"[!] Re-login failed: {e}")
                        return False
                    continue
                print("[!] Re-login already attempted - giving up.")
                return False

            elif 400 <= response.status_code < 500:
                # Client error - don't retry, configuration issue
                print(f"[!] IBO Player API error {response.status_code}: {response.text[:200]}")
                print(f"[!] This is a configuration error - please check your IBO Player credentials")
                return False

            else:
                # Server error - retry with exponential backoff
                print(f"[!] IBO Player API error {response.status_code} (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s, 8s
                    print(f"[*] Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)

        except requests.exceptions.Timeout:
            print(f"[!] IBO Player API timeout (attempt {attempt}/{max_retries})")
            if attempt < max_retries:
                wait_time = 2 ** attempt
                print(f"[*] Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

        except Exception as e:
            print(f"[!] IBO Player API exception (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                wait_time = 2 ** attempt
                print(f"[*] Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

    print(f"[!] Failed to save playlist to IBO Player after {max_retries} attempts")
    return False


# ═══════════════════════════════════════════════════════════
# Webhook & Notification Integration
# ═══════════════════════════════════════════════════════════

def send_webhook_callback(callback_url, user_id, status, username=None, password=None, host=None, m3u_url=None, error=None, max_retries=3):
    """Send webhook callback to Laravel backend."""
    if not callback_url:
        print("[*] No callback URL provided, skipping webhook")
        return False

    payload = {
        "user_id": user_id,
        "status": status,
        "timestamp": datetime.now().isoformat(),
    }
    if status == "success":
        payload.update({
            "username": username,
            "password": password,
            "host": host,
            "m3u_url": m3u_url
        })
    else:
        payload["error"] = error

    headers = {"Content-Type": "application/json", "User-Agent": "IPTVV-Canada-Automation/1.0"}
    webhook_token = os.getenv("WEBHOOK_AUTH_TOKEN", "")
    if webhook_token:
        headers["Authorization"] = f"Bearer {webhook_token}"

    print(f"[*] Sending webhook: {json.dumps({**payload, 'password': '***' if password else None}, indent=2)}")
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(callback_url, json=payload, headers=headers, timeout=30)
            print(f"[*] Webhook response status: {response.status_code}")
            if response.status_code in (200, 201, 202):
                print("[OK] Webhook sent successfully")
                return True
            if 400 <= response.status_code < 500:
                print(f"[!] Webhook client error: {response.text[:500]}")
                return False
        except requests.RequestException as exc:
            print(f"[!] Webhook request failed: {exc}")
        if attempt < max_retries:
            time.sleep(2 ** attempt)
    return False


def send_telegram_notification(status, message, details=None):
    """Send Telegram notification using the notifier module."""
    try:
        if status == "success":
            notifier.notify_success(message, details, None)
        else:
            notifier.notify_error(message, details, None)
    except Exception as exc:
        print(f"[!] Telegram notification failed: {exc}")


# ═══════════════════════════════════════════════════════════
# Main Automation Flow
# ═══════════════════════════════════════════════════════════

def run_automation():
    """Execute the full trial-creation flow and return the extracted credentials.

    Runs the WooCommerce checkout, waits for the credentials email, extracts the
    Xtream credentials and (if enabled) saves them to IBO Player. Raises on any
    failure. The caller owns webhook/Telegram notifications and result reporting,
    so this function is reusable by both the CLI and the Apify Actor wrapper.

    Returns:
        dict: {email, username, password, host, m3u_url}
    """
    driver = None
    try:
        print("\n[*] Creating trial using public IP (direct connection)")
        print("=" * 60)

        # Step 1: Initialize browser and confirm IPTVV checkout is reachable.
        driver = get_driver()

        # Step 2: Navigate to cart and start trial process.
        navigate_to_cart_and_get_free_trial(driver)

        # Step 3: Select full channel package.
        select_full_channel_package(driver)

        # Step 4: Allocate a receiving address only after the checkout form is reachable.
        email_session = create_email_session(driver)
        if not email_session:
            raise RuntimeError(f"Failed to create temporary email ({IPTVV_EMAIL_BACKEND} backend)")
        email_address = email_session["address"]

        # Step 5: Fill checkout form with the temporary email
        fill_checkout_form(driver, email_address)

        # Step 6: Submit form
        submit_checkout_form(driver)

        # Step 7: Wait for credentials email (this can take 5-45 minutes)
        print("\n" + "=" * 60)
        print(f"[*] Order submitted! Monitoring {IPTVV_EMAIL_BACKEND} inbox: {email_address}")
        print("=" * 60 + "\n")

        credentials_message = wait_for_credentials_email(driver, email_session)
        if not credentials_message:
            raise RuntimeError(f"Timeout: Credentials email not received after {EMAIL_MAX_WAIT_SECONDS} seconds")

        # Step 8: Extract credentials from email
        username, password, hostname = extract_credentials_from_email(credentials_message)

        if not username or not password or not hostname:
            raise RuntimeError("Failed to extract complete credentials from email")

        # Construct M3U URL
        m3u_url = f"{hostname}/get.php?username={username}&password={password}&type=m3u_plus&output=ts"

        # Success!
        print("\n" + "=" * 60)
        print("\u2713 IPTVV CANADA CREDENTIALS EXTRACTED SUCCESSFULLY")
        print("=" * 60)
        print(f"[*] Server Address: {hostname}")
        print(f"[*] Username: {username}")
        print(f"[*] Password: {password}")
        print(f"[*] M3U URL: {m3u_url}")
        print("=" * 60 + "\n")

        # Save to IBO Player if enabled
        save_to_iboplayer(username, password, hostname)

        return {
            "email": email_address,
            "username": username,
            "password": password,
            "host": hostname,
            "m3u_url": m3u_url,
        }

    finally:
        if driver and AUTO_EXIT:
            try:
                driver.quit()
                print("[*] Browser closed")
            except:
                pass
        elif driver:
            print("[*] AUTO_EXIT disabled; browser will remain open")
            while True:
                time.sleep(1)


def main(user_id=None, callback_url=None):
    """Main automation workflow (CLI entry point)."""
    is_laravel_mode = bool(user_id and callback_url)

    print("\n" + "=" * 60)
    print("IPTVV CANADA - AUTOMATED TRIAL CREATION")
    print("=" * 60)
    print(f"[*] User ID: {user_id if user_id else 'N/A'}")
    print(f"[*] Callback URL: {callback_url if callback_url else 'N/A'}")
    print(f"[*] Laravel integration mode: {is_laravel_mode}")
    print("=" * 60 + "\n")

    try:
        result = run_automation()

        # Send success notifications
        if is_laravel_mode:
            send_webhook_callback(
                callback_url=callback_url,
                user_id=user_id,
                status="success",
                username=result["username"],
                password=result["password"],
                host=result["host"],
                m3u_url=result["m3u_url"],
            )

        send_telegram_notification(
            "success",
            f"IPTVV Canada trial created for {result['email']}",
            f"Username: {result['username']}\nHost: {result['host']}"
        )

        print("[OK] IPTVV Canada automation complete")

    except (CloudflareBlockedError, TrialRejectedError) as exc:
        # No proxy fallback configured - report on the public IP result and exit.
        print(f"\n[!] IPTVV Canada automation failed: {exc}")

        if is_laravel_mode:
            send_webhook_callback(
                callback_url=callback_url,
                user_id=user_id,
                status="failed",
                error=str(exc)
            )
        send_telegram_notification("error", type(exc).__name__, str(exc))
        raise SystemExit(1)

    except Exception as exc:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"\n[!] IPTVV Canada automation failed: {exc}")
        print(error_traceback)

        if is_laravel_mode:
            send_webhook_callback(
                callback_url=callback_url,
                user_id=user_id,
                status="failed",
                error=f"{exc}\n\n{error_traceback}"
            )

        send_telegram_notification("error", str(exc), error_traceback)
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="IPTVV Canada - Automated Trial Account Creation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python iptvvcanada_automation.py
  python iptvvcanada_automation.py --preflight-only
  python iptvvcanada_automation.py --user-id 123 --callback-url https://app.com/api/webhooks/iptvv-automation
        """,
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Only verify IPTVV checkout reachability and browser egress; do not create a trial",
    )
    parser.add_argument("--user-id", type=int, help="Laravel IPTV account ID")
    parser.add_argument("--callback-url", type=str, help="Webhook callback URL")
    args = parser.parse_args()

    if args.preflight_only:
        try:
            preflight_checkout_access()
        except Exception as exc:
            import traceback
            error_traceback = traceback.format_exc()
            print(f"\n[!] IPTVV Canada preflight failed: {exc}")
            print(error_traceback)
            raise SystemExit(1)
    else:
        main(user_id=args.user_id, callback_url=args.callback_url)
