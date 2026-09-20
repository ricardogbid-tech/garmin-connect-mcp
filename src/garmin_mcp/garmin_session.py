"""Redis-backed Garmin sessions for the single-instance Render deployment.

Only the three DI token fields from garminconnect 0.3.6 are persisted. The
GitHub/MCP OAuth store and its TOKEN_STORE_KEY are intentionally independent.
"""

import base64
import json
import logging
import os

import httpx
from garminconnect import Garmin

logger = logging.getLogger(__name__)

SESSION_KEY = "mcp:garmin:session"
TOKEN_FIELDS = {"di_token", "di_refresh_token", "di_client_id"}


class GarminSessionError(RuntimeError):
    """A session failure whose message is safe to log or return through MCP."""


class _SafeSessionLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 0.3.6 interpolates the entire tokenstore into this DEBUG message.
        if str(record.msg).startswith((
            "Failed to cleanly load tokens from ",
            "DI token refresh failed:",
            "Refresh failed:",
            "Login failed:",
            "Retrying social profile fetch:",
            "Retrying user settings fetch:",
        )):
            record.msg = "Garmin session operation failed (token details redacted)."
            record.args = ()
            record.exc_info = None
        return True


logging.getLogger("garminconnect").addFilter(_SafeSessionLog())
logging.getLogger("garminconnect.client").addFilter(_SafeSessionLog())


def _validate_session(raw: str, source: str) -> str:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != TOKEN_FIELDS:
            raise ValueError
        if not all(isinstance(value, str) and value.strip() for value in data.values()):
            raise ValueError
    except (TypeError, ValueError):
        raise GarminSessionError(
            f"Invalid Garmin session in {source}; refusing bootstrap fallback."
        ) from None
    return json.dumps(data)


class GarminSessionStore:
    def __init__(self) -> None:
        self.url = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
        self.token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
        if not self.url.startswith("https://") or not self.token:
            raise GarminSessionError(
                "Garmin session requires UPSTASH_REDIS_REST_URL (HTTPS) and "
                "UPSTASH_REDIS_REST_TOKEN."
            )

    def _command(self, command: str, *args: str):
        try:
            with httpx.Client(timeout=5) as http:
                response = http.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    json=[command, SESSION_KEY, *args],
                )
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict) or "error" in body or "result" not in body:
                raise ValueError
            result = body["result"]
            if command == "SET" and result != "OK":
                raise ValueError
            if command == "GET" and result is not None and not isinstance(result, str):
                raise ValueError
            return result
        except Exception:
            # Never include a response body, request or exception: these may
            # contain the session or Redis credentials.
            raise GarminSessionError(
                f"Garmin session Redis {command} failed; no bootstrap fallback."
            ) from None

    def load(self) -> str | None:
        return self._command("GET")

    def save(self, raw: str) -> None:
        self._command("SET", _validate_session(raw, "client"))


def _persist_refreshes(garmin: Garmin, store: GarminSessionStore) -> None:
    client = garmin.client
    original_refresh = client._refresh_session
    original_di_refresh = client._refresh_di_token
    original_request = client._run_request
    pending = False

    def save() -> None:
        nonlocal pending
        pending = True
        try:
            store.save(client.dumps())
        except GarminSessionError:
            logger.error(
                "Garmin session persistence failed; renewed tokens remain in memory. "
                "Will retry persistence before the next Garmin request."
            )
            raise
        pending = False

    def refresh_di_and_save() -> None:
        original_di_refresh()
        # Only reached on a successful DI refresh, even if Garmin returns the
        # same token values. Persist before the library makes another API call.
        save()

    def refresh_and_save() -> None:
        # Do not rotate again while a previous successful refresh is unsaved.
        if pending:
            save()
        original_refresh()
        # 0.3.6 swallows exceptions from _refresh_di_token(), including a Redis
        # write failure. Surface that failure outside its exception handler.
        if pending:
            raise GarminSessionError("Garmin refreshed but Redis persistence failed.")

    def request_with_pending_save(*args, **kwargs):
        # Retry storage before sending any API request, even with a valid access
        # token. If Redis still fails, leave it pending and do not send/replay
        # the Garmin operation (which may be a POST, PUT or DELETE).
        if pending:
            save()
        return original_request(*args, **kwargs)

    # Private integration points: deliberately pinned to garminconnect==0.3.6.
    # Installed before login, which can itself refresh the loaded session.
    client._refresh_session = refresh_and_save
    client._refresh_di_token = refresh_di_and_save
    client._run_request = request_with_pending_save


def load_garmin_session(*, is_cn: bool = False) -> Garmin:
    store = GarminSessionStore()
    raw = store.load()
    source = "Redis"
    if raw is None:
        source = "GARMINTOKENS_BASE64"
        bootstrap = os.getenv("GARMINTOKENS_BASE64", "")
        if not bootstrap:
            raise GarminSessionError(
                "No Garmin session in Redis; set GARMINTOKENS_BASE64 to bootstrap."
            )
        try:
            raw = base64.b64decode(bootstrap, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise GarminSessionError("Invalid GARMINTOKENS_BASE64 encoding.") from None

    raw = _validate_session(raw, source)
    garmin = Garmin(is_cn=is_cn)
    _persist_refreshes(garmin, store)
    try:
        # 0.3.6 interprets strings <=512 characters as filesystem paths. Padding
        # with JSON whitespace ensures even malformed short tokens stay data.
        garmin.login(raw.ljust(513))
    except Exception:
        # login() can wrap persistence errors or authentication errors. Do not
        # log its exception text; the original may include sensitive data.
        raise GarminSessionError(
            f"Garmin session login from {source} failed; no bootstrap fallback. "
            "Check Redis availability and Garmin session validity."
        ) from None
    # Also seed Redis after a successful login that did not need a refresh.
    store.save(garmin.client.dumps())
    logger.info("Garmin session loaded from %s and persisted in Redis.", source)
    return garmin
