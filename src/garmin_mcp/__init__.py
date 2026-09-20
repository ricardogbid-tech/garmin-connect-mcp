import base64
import datetime
import json
import logging
import os
import sys

import anyio
import uvicorn
from garminconnect import Garmin
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse

from garmin_mcp import (
    activity_management,
    challenges,
    data_management,
    devices,
    gear_management,
    health_wellness,
    nutrition,
    training,
    user_profile,
    weight_management,
    womens_health,
    workout_templates,
    workouts,
)
from garmin_mcp.github_oauth_provider import GitHubOAuthProvider
from garmin_mcp.garmin_session import GarminSessionError, load_garmin_session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

is_cn = os.getenv("GARMIN_IS_CN", "false").lower() == "true"

SERVER_URL = os.getenv("SERVER_URL", "")

_MODULES = [
    activity_management,
    challenges,
    data_management,
    devices,
    gear_management,
    health_wellness,
    nutrition,
    training,
    user_profile,
    weight_management,
    womens_health,
    workouts,
]


def _build_app() -> tuple[FastMCP, GitHubOAuthProvider]:
    github_client_id = os.getenv("GITHUB_CLIENT_ID", "")
    github_client_secret = os.getenv("GITHUB_CLIENT_SECRET", "")

    if not github_client_id or not github_client_secret:
        logger.error("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET must be set.")
        sys.exit(1)

    if not SERVER_URL:
        logger.error("SERVER_URL must be set to the public base URL of this service.")
        sys.exit(1)

    oauth_provider = GitHubOAuthProvider(
        github_client_id=github_client_id,
        github_client_secret=github_client_secret,
        server_url=SERVER_URL,
    )

    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(SERVER_URL),
        resource_server_url=AnyHttpUrl(SERVER_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["mcp"],
            default_scopes=["mcp"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    mcp_app = FastMCP(
        "Garmin Connect MCP",
        auth_server_provider=oauth_provider,
        auth=auth_settings,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )

    # GitHub redirects here after user login
    @mcp_app.custom_route("/auth/callback", methods=["GET"])
    async def github_callback(request: Request):
        return await oauth_provider.handle_github_callback(request)

    @mcp_app.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp_app, oauth_provider


def _log_token_expiry(garmin: Garmin) -> None:
    try:
        # Diagnostic only: the JWT exp belongs to the ACCESS token, not the
        # refresh token. garminconnect 0.3.6 does not expose refresh expiry.
        payload = garmin.client.di_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expires_at = datetime.datetime.fromtimestamp(
            claims["exp"], tz=datetime.timezone.utc
        )
        logger.info("Garmin access token expiry: %s; automatic refresh enabled.", expires_at.isoformat())
    except Exception:
        logger.info("Garmin access token expiry unavailable; automatic refresh enabled.")


def init_api() -> Garmin:
    try:
        garmin = load_garmin_session(is_cn=is_cn)
    except GarminSessionError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    _log_token_expiry(garmin)
    return garmin


async def _serve(mcp_app: FastMCP) -> None:
    starlette_app = mcp_app.streamable_http_app()
    config = uvicorn.Config(
        starlette_app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        log_level="info",
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    garmin = init_api()
    mcp_app, _ = _build_app()

    for module in _MODULES:
        module.configure(garmin)
        module.register_tools(mcp_app)

    workout_templates.register_resources(mcp_app)

    logger.info("GitHub OAuth enabled — only '%s' can authenticate.", os.getenv("GITHUB_ALLOWED_USER", "(not configured)"))
    anyio.run(_serve, mcp_app)
