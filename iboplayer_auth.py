#!/usr/bin/env python3
"""
IboPlayer device-login helper.

IboPlayer's frontend endpoints (savePlaylist, ...) used to be authenticated with a
static session cookie. That cookie now expires quickly; the site requires a
**Bearer token** obtained by logging the device in:

    1. GET  https://iboplayer.com/frontend/captcha/generate  -> {"svg": "...", "token": "..."}
    2. Solve the 2-letter text captcha drawn inside the SVG.
    3. POST https://iboplayer.com/frontend/device/login
           {"mac_address": ..., "device_key": ..., "captcha": <text>, "token": <captcha token>}
       -> returns a bearer token (JWT, ~24h).
    4. Call the frontend API with `Authorization: Bearer <token>`.

The captcha is solved with 2captcha's normal (image) solver. The SVG is rasterized
to PNG with cairosvg first, since 2captcha needs a raster image.

The mac_address/device_key identify the IBO Player device. They can be passed to
get_bearer_token()/authed_headers() by the caller, or default to the
IBOPLAYER_MAC_ADDRESS / IBOPLAYER_DEVICE_KEY environment variables.

Public surface:
    get_bearer_token(mac_address=None, device_key=None, force_refresh=False) -> str
    authed_headers(mac_address=None, device_key=None, extra=None, force_refresh=False) -> dict
    clear_cached_token(mac_address=None)

Run `python iboplayer_auth.py` to exercise the full login flow.

Environment variables:
    IBOPLAYER_MAC_ADDRESS   - default device MAC address (e.g. b8:13:a0:e0:6e:83)
    IBOPLAYER_DEVICE_KEY    - default device key (e.g. 723486)
    TWOCAPTCHA_API_KEY      - 2captcha API key for solving the captcha
    IBOPLAYER_COOKIE        - (optional/legacy) sent alongside the bearer if set
"""

import os
import re
import json
import tempfile
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CAPTCHA_URL = "https://iboplayer.com/frontend/captcha/generate"
LOGIN_URL = "https://iboplayer.com/frontend/device/login"

# Shared User-Agent used across the repo's iboplayer requests.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

IBOPLAYER_MAC_ADDRESS = os.getenv("IBOPLAYER_MAC_ADDRESS")
IBOPLAYER_DEVICE_KEY = os.getenv("IBOPLAYER_DEVICE_KEY")
TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
IBOPLAYER_COOKIE = os.getenv("IBOPLAYER_COOKIE")

# How many (generate captcha -> solve -> login) attempts before giving up.
# 2captcha misreads these distorted 2-letter captchas ~half the time, so allow a
# few retries; each retry uses a fresh captcha.
MAX_LOGIN_ATTEMPTS = 5

# Common places the bearer token may live in the login JSON response.
_TOKEN_KEYS = ("token", "access_token", "accessToken", "bearer", "jwt")


class IboPlayerAuthError(Exception):
    """Raised when the device-login flow cannot produce a bearer token."""


def _resolve_identity(mac_address, device_key):
    mac = mac_address or IBOPLAYER_MAC_ADDRESS
    dk = device_key or IBOPLAYER_DEVICE_KEY
    return mac, dk


# ---------------------------------------------------------------------------
# Token cache (keyed by mac so different devices don't collide)
# ---------------------------------------------------------------------------

def _cache_path(mac_address):
    safe = re.sub(r"[^A-Za-z0-9]", "", mac_address or "default")
    return f"iboplayer_token_{safe}.json"


def _read_cache(mac_address):
    path = _cache_path(mac_address)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("token") or None
    except Exception as e:
        print(f"[!] Could not read token cache: {e}")
        return None


def _write_cache(mac_address, token):
    path = _cache_path(mac_address)
    try:
        with open(path, "w") as f:
            json.dump({"token": token, "obtained_at": datetime.now().isoformat()}, f, indent=2)
        print(f"[✓] Cached bearer token to {path}")
    except Exception as e:
        print(f"[!] Could not write token cache: {e}")


def clear_cached_token(mac_address=None):
    """Delete the cached bearer token for a device (call after a 401/403)."""
    mac, _ = _resolve_identity(mac_address, None)
    path = _cache_path(mac)
    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"[*] Cleared cached token ({path})")
    except Exception as e:
        print(f"[!] Could not clear token cache: {e}")


# ---------------------------------------------------------------------------
# Captcha + login internals
# ---------------------------------------------------------------------------

def _new_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    })
    return session


def _generate_captcha(session):
    """GET the captcha endpoint. Returns (svg_markup, captcha_token)."""
    resp = session.get(CAPTCHA_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    svg = data.get("svg")
    token = data.get("token")
    if not svg or not token:
        raise IboPlayerAuthError(f"Unexpected captcha response: {data!r}")
    return svg, token


def _sanitize_svg(svg_markup):
    """
    IboPlayer's captcha <svg> tag carries duplicate attributes
    (e.g. width="200"...width="150"), which is invalid XML and rejected by the
    strict parser cairosvg uses. Deduplicate attributes on the root <svg> tag,
    keeping the first occurrence of each.
    """
    m = re.match(r"(<svg\b)(.*?)(>)", svg_markup, re.DOTALL)
    if not m:
        return svg_markup
    seen = {}
    for name, val in re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', m.group(2)):
        if name not in seen:
            seen[name] = val
    # The conflicting width/height (e.g. 200x120) stretch and pad the real 150x50
    # viewBox. Drop them when a viewBox exists so the natural aspect ratio is used.
    if "viewBox" in seen:
        seen.pop("width", None)
        seen.pop("height", None)
    rebuilt = m.group(1) + "".join(f' {n}="{v}"' for n, v in seen.items()) + m.group(3)
    return svg_markup[:m.start()] + rebuilt + svg_markup[m.end():]


def _rasterize_svg(svg_markup):
    """Render SVG markup to PNG bytes. Upscaled for solver accuracy."""
    import cairosvg  # imported lazily so the module loads even if cairo is missing
    clean = _sanitize_svg(svg_markup)
    return cairosvg.svg2png(bytestring=clean.encode("utf-8"), output_width=400)


def _solve_captcha(png_bytes):
    """Send the rasterized captcha image to 2captcha and return the solved text."""
    from twocaptcha import TwoCaptcha

    if not TWOCAPTCHA_API_KEY:
        raise IboPlayerAuthError("TWOCAPTCHA_API_KEY is not set")

    solver = TwoCaptcha(TWOCAPTCHA_API_KEY)

    # 2captcha's normal() solver wants a file path; write the PNG to a temp file.
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(png_bytes)
        tmp_path = tmp.name
    try:
        result = solver.normal(
            tmp_path,
            numeric=2,          # 2 = letters only
            minLen=2,
            maxLen=2,
            caseSensitive=1,    # captchas like "ZV" appear uppercase
            hintText="Enter the 2 letters shown in the image",
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    code = (result.get("code") or "").strip()
    if not code:
        raise IboPlayerAuthError(f"2captcha returned no code: {result!r}")
    # The captcha answer is case-sensitive and uppercase (verified against the live
    # /device/login endpoint: lowercase is rejected, uppercase accepted).
    return code.upper()


def _extract_token(login_json):
    """Pull the bearer token out of the /device/login response."""
    if not isinstance(login_json, dict):
        return None
    for key in _TOKEN_KEYS:
        val = login_json.get(key)
        if isinstance(val, str) and val:
            return val
    data = login_json.get("data")
    if isinstance(data, dict):
        for key in _TOKEN_KEYS:
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _login(session, mac_address, device_key):
    """Run the generate -> solve -> login loop until a bearer token is obtained."""
    last_error = None
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        print(f"\n[*] Login attempt {attempt}/{MAX_LOGIN_ATTEMPTS}")
        try:
            svg, captcha_token = _generate_captcha(session)
            print("[*] Captcha generated, rasterizing and solving...")
            png_bytes = _rasterize_svg(svg)
            captcha_text = _solve_captcha(png_bytes)
            print(f"[*] Captcha solved as: {captcha_text!r}")

            payload = {
                "mac_address": mac_address,
                "device_key": device_key,
                "captcha": captcha_text,
                "token": captcha_token,
            }
            resp = session.post(LOGIN_URL, json=payload, timeout=30)
            print(f"[*] /device/login status: {resp.status_code}")

            try:
                body = resp.json()
            except ValueError:
                body = None

            if resp.status_code == 200:
                token = _extract_token(body)
                if token:
                    print("[✓] Bearer token obtained")
                    return token
                last_error = f"200 OK but no token found in response: {body!r}"
                print(f"[!] {last_error}")
            else:
                last_error = f"login returned {resp.status_code}: {resp.text[:300]}"
                print(f"[!] {last_error} (likely wrong captcha) - retrying with a fresh captcha")

        except IboPlayerAuthError:
            raise
        except Exception as e:
            last_error = str(e)
            print(f"[!] Login attempt failed: {e}")

    raise IboPlayerAuthError(f"Could not log in after {MAX_LOGIN_ATTEMPTS} attempts. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_bearer_token(mac_address=None, device_key=None, force_refresh=False):
    """
    Return a valid bearer token for the given device, logging in if necessary.

    mac_address/device_key default to the IBOPLAYER_MAC_ADDRESS / IBOPLAYER_DEVICE_KEY
    environment variables. Uses the cached token unless force_refresh=True.
    """
    mac, dk = _resolve_identity(mac_address, device_key)

    if not force_refresh:
        cached = _read_cache(mac)
        if cached:
            print("[*] Using cached bearer token")
            return cached

    if not mac or not dk:
        raise IboPlayerAuthError(
            "mac_address and device_key must be provided (or set IBOPLAYER_MAC_ADDRESS / "
            "IBOPLAYER_DEVICE_KEY) to log in"
        )

    print("[*] Logging in to iboplayer.com to obtain a bearer token...")
    session = _new_session()
    token = _login(session, mac, dk)
    _write_cache(mac, token)
    return token


def authed_headers(mac_address=None, device_key=None, extra=None, force_refresh=False):
    """
    Build request headers authenticated with a bearer token.

    The legacy IBOPLAYER_COOKIE, if still set, is sent alongside the bearer in case
    savePlaylist continues to require it.
    """
    token = get_bearer_token(mac_address, device_key, force_refresh=force_refresh)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
    }
    if IBOPLAYER_COOKIE:
        headers["Cookie"] = IBOPLAYER_COOKIE
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# Manual test / verification entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 70)
    print("IboPlayer device-login test")
    print("=" * 70)
    print(f"[*] MAC: {IBOPLAYER_MAC_ADDRESS}")
    print(f"[*] Device key: {IBOPLAYER_DEVICE_KEY}")
    print(f"[*] 2captcha key set: {bool(TWOCAPTCHA_API_KEY)}")

    try:
        clear_cached_token()
        token = get_bearer_token(force_refresh=True)
        print(f"\n[✓] Bearer token: {token[:40]}...")
    except Exception as e:
        print(f"\n[✗] Login failed: {e}")
