"""Apify Actor wrapper around the IPTVV Canada trial automation.

Reads the Actor input, maps it onto the environment variables that
``iptvvcanada_automation`` reads at import time, runs the (synchronous,
Selenium-based) flow in a worker thread, and reports the result to the Apify
dataset / key-value store and, optionally, to a webhook callback.
"""
import asyncio
import os

from apify import Actor

# Maps Actor input keys -> the env vars iptvvcanada_automation reads at import.
# Only non-empty values are applied so the module's own defaults still win.
_ENV_MAP = {
    "twoCaptchaApiKey": "TWOCAPTCHA_API_KEY",
    "webhookAuthToken": "WEBHOOK_AUTH_TOKEN",
    "emailBackend": "IPTVV_EMAIL_BACKEND",
    "tmailyDomain": "TMAILY_DOMAIN",
    "gmailAddress": "IPTVV_GMAIL_ADDRESS",
    "gmailAppPassword": "IPTVV_GMAIL_APP_PASSWORD",
    "baseUrl": "IPTVV_BASE_URL",
    "emailMaxWaitSeconds": "IPTVV_EMAIL_MAX_WAIT_SECONDS",
    "emailPollSeconds": "IPTVV_EMAIL_POLL_SECONDS",
    "cloudflareWaitSeconds": "IPTVV_CLOUDFLARE_WAIT_SECONDS",
    "iboPlayerCookie": "IPTVV_IBOPLAYER_COOKIE",
    "iboPlayerMacAddress": "IPTVV_IBOPLAYER_MAC_ADDRESS",
    "iboPlayerDeviceKey": "IPTVV_IBOPLAYER_DEVICE_KEY",
    "iboPlayerPlaylistUrlId": "IPTVV_IBOPLAYER_PLAYLIST_URL_ID",
    "iboPlayerPlaylistName": "IPTVV_IBOPLAYER_PLAYLIST_NAME",
}


def _apply_input_to_env(actor_input):
    """Translate Actor input into the env vars the automation module expects."""
    for key, env_name in _ENV_MAP.items():
        value = actor_input.get(key)
        if value not in (None, ""):
            os.environ[env_name] = str(value)

    if actor_input.get("iboPlayerEnabled"):
        os.environ["IPTVV_IBOPLAYER_ENABLED"] = "True"

    # Apify always runs headless and should never hang waiting for a browser.
    os.environ["HEADLESS"] = "True"
    os.environ["AUTO_EXIT"] = "True"
    # Apify containers can't write to /app; use a writable scratch dir for artifacts.
    os.environ.setdefault("IPTVV_DEBUG_DIR", "/tmp/iptvv-logs")


async def main():
    async with Actor:
        actor_input = await Actor.get_input() or {}
        _apply_input_to_env(actor_input)

        user_id = actor_input.get("userId")
        callback_url = actor_input.get("callbackUrl")

        # Import only AFTER env vars are set: the module reads them at import time.
        import iptvvcanada_automation as bot

        try:
            Actor.log.info("Starting IPTVV Canada trial automation...")
            result = await asyncio.to_thread(bot.run_automation)
        except (bot.CloudflareBlockedError, bot.TrialRejectedError) as exc:
            await _report_failure(bot, callback_url, user_id, exc)
            await Actor.fail(status_message=f"{type(exc).__name__}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - surface any failure to the run
            Actor.log.exception("Automation failed")
            await _report_failure(bot, callback_url, user_id, exc)
            await Actor.fail(status_message=f"Automation failed: {exc}")
            return

        Actor.log.info("Credentials extracted successfully")
        output = {"status": "success", **result}
        await Actor.push_data(output)
        await Actor.set_value("OUTPUT", output)

        if callback_url:
            await asyncio.to_thread(
                bot.send_webhook_callback,
                callback_url,
                user_id,
                "success",
                result["username"],
                result["password"],
                result["host"],
                result["m3u_url"],
            )


async def _report_failure(bot, callback_url, user_id, exc):
    """Persist the failure to the dataset/KV store and fire the webhook."""
    output = {"status": "failed", "error": str(exc)}
    await Actor.push_data(output)
    await Actor.set_value("OUTPUT", output)
    if callback_url:
        await asyncio.to_thread(
            bot.send_webhook_callback,
            callback_url,
            user_id,
            "failed",
            None,
            None,
            None,
            None,
            str(exc),
        )
