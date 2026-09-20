"""Exercise the real 0.3.6 refresh flow with fake Garmin/Upstash responses."""

import base64
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from garminconnect import Garmin
from garminconnect.client import Client

from garmin_mcp import _log_token_expiry, init_api
from garmin_mcp.garmin_session import (
    SESSION_KEY,
    TOKEN_FIELDS,
    GarminSessionError,
    load_garmin_session,
)


def session(label="bootstrap", *, expired=False):
    # login() treats strings <=512 characters as paths. Match real JWT sizes.
    claims = {
        "exp": int(time.time()) + (-60 if expired else 7200),
        "client_id": "test-client",
        "padding": "x" * 600,
        "label": label,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return json.dumps({
        "di_token": f"test.{payload}.signature",
        "di_refresh_token": f"secret-refresh-{label}",
        "di_client_id": "test-client",
    })


@pytest.fixture
def redis(monkeypatch):
    state = SimpleNamespace(value=None, writes=[], read_error=False, write_error=False, reply=None)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://redis.example.test")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "secret-redis-auth")
    monkeypatch.setenv("TOKEN_STORE_KEY", "existing-mcp-oauth-key")
    monkeypatch.setenv("GARMINTOKENS_BASE64", base64.b64encode(session().encode()).decode())

    def handle(request):
        assert request.headers["Authorization"] == "Bearer secret-redis-auth"
        command, key, *args = json.loads(request.content)
        assert key == SESSION_KEY
        if state.reply is not None:
            return httpx.Response(200, json=state.reply)
        if command == "GET":
            if state.read_error:
                raise httpx.ConnectError("secret-redis-auth", request=request)
            return httpx.Response(200, json={"result": state.value})
        assert command == "SET"
        if state.write_error:
            return httpx.Response(503, json={"error": "secret-redis-auth"})
        state.value = args[0]
        state.writes.append(args[0])
        return httpx.Response(200, json={"result": "OK"})

    http_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: http_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    # No credentials or real Garmin requests in this suite. Retain real login,
    # token parsing and refresh code; mock only profile retrieval and HTTP.
    monkeypatch.setattr(Garmin, "_load_profile_and_settings", Mock())
    monkeypatch.setattr(Client, "_http_post", Mock(side_effect=AssertionError("Unexpected Garmin HTTP")))
    return state


def mock_refresh(monkeypatch, raw):
    data = json.loads(raw)
    response = Mock(ok=True)
    response.json.return_value = {
        "access_token": data["di_token"],
        "refresh_token": data["di_refresh_token"],
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(Client, "_http_post", post)
    return post


def test_bootstrap_from_env(redis):
    garmin = load_garmin_session()
    assert garmin.client.di_refresh_token == "secret-refresh-bootstrap"
    assert json.loads(redis.value) == json.loads(garmin.client.dumps())
    assert set(json.loads(redis.value)) == TOKEN_FIELDS
    assert garmin.username is None
    assert garmin.password is None


def test_redis_has_priority_over_even_invalid_env(redis, monkeypatch):
    redis.value = session("redis")
    monkeypatch.setenv("GARMINTOKENS_BASE64", "not base64")
    garmin = load_garmin_session()
    assert garmin.client.di_refresh_token == "secret-refresh-redis"


def test_refresh_immediately_persists_before_api_request(redis, monkeypatch):
    garmin = load_garmin_session()
    renewed = session("renewed")
    post = mock_refresh(monkeypatch, renewed)
    garmin.client.di_token = json.loads(session(expired=True))["di_token"]

    def api_request(*args, **kwargs):
        assert json.loads(redis.value) == json.loads(renewed)
        return Mock(status_code=200, json=Mock(return_value={"ok": True}))

    monkeypatch.setattr(garmin.client._api_session, "request", api_request)
    assert garmin.client.connectapi("/test") == {"ok": True}
    assert post.call_count == 1


def test_restart_uses_persisted_refresh_not_original_bootstrap(redis, monkeypatch):
    first = load_garmin_session()
    mock_refresh(monkeypatch, session("renewed"))
    first.client._refresh_session()
    second = load_garmin_session()
    assert second.client.di_refresh_token == "secret-refresh-renewed"
    assert second.client.dumps() == first.client.dumps()


def test_refresh_during_login_is_persisted_before_profile_load(redis, monkeypatch):
    redis.value = session("expired", expired=True)
    renewed = session("renewed")
    mock_refresh(monkeypatch, renewed)

    def profile(_self):
        assert json.loads(redis.value) == json.loads(renewed)

    monkeypatch.setattr(Garmin, "_load_profile_and_settings", profile)
    load_garmin_session()


def test_successful_refresh_with_unchanged_tokens_still_persists(redis, monkeypatch):
    garmin = load_garmin_session()
    mock_refresh(monkeypatch, garmin.client.dumps())
    redis.writes.clear()
    garmin.client._refresh_session()
    assert redis.writes == [garmin.client.dumps()]


def test_401_refresh_is_persisted_before_retry(redis, monkeypatch):
    garmin = load_garmin_session()
    renewed = session("after-401")
    mock_refresh(monkeypatch, renewed)
    calls = []

    def api_request(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Mock(status_code=401)
        assert json.loads(redis.value) == json.loads(renewed)
        return Mock(status_code=200, json=Mock(return_value={"ok": True}))

    monkeypatch.setattr(garmin.client._api_session, "request", api_request)
    assert garmin.client.connectapi("/test") == {"ok": True}
    assert len(calls) == 2


def test_redis_read_failure_stops_startup_and_logs_safely(redis, caplog):
    redis.read_error = True
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
        init_api()
    assert "Redis GET failed" in caplog.text
    assert "secret-redis-auth" not in caplog.text
    assert "secret-refresh" not in caplog.text
    assert redis.writes == []
    Garmin._load_profile_and_settings.assert_not_called()


@pytest.mark.parametrize("reply", [
    {"error": "secret-redis-auth"}, {}, {"result": 123}, [],
])
def test_invalid_redis_response_is_not_treated_as_missing(redis, reply):
    redis.reply = reply
    with pytest.raises(GarminSessionError, match="Redis GET failed"):
        load_garmin_session()
    assert redis.writes == []
    Garmin._load_profile_and_settings.assert_not_called()


@pytest.mark.parametrize("raw", [
    "", "secret-corrupt-json", "null", "[]", "{}",
    json.dumps({"di_token": "secret-corrupt-token"}),
    json.dumps({"di_token": "x", "di_refresh_token": None, "di_client_id": "test"}),
    json.dumps({"di_token": "x", "di_refresh_token": " ", "di_client_id": "test"}),
    json.dumps({**json.loads(session()), "password": "secret-password"}),
])
def test_corrupt_redis_never_falls_back(redis, raw, caplog):
    redis.value = raw
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
        init_api()
    assert "Invalid Garmin session in Redis" in caplog.text
    assert "secret-" not in caplog.text
    assert redis.value == raw
    assert redis.writes == []
    Garmin._load_profile_and_settings.assert_not_called()


def test_missing_redis_configuration_never_uses_bootstrap(redis, monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN")
    with pytest.raises(GarminSessionError, match="requires UPSTASH"):
        load_garmin_session()
    assert redis.writes == []


@pytest.mark.parametrize("bootstrap", ["", "not base64", base64.b64encode(b"{}").decode()])
def test_missing_or_invalid_bootstrap_is_rejected(redis, monkeypatch, bootstrap):
    monkeypatch.setenv("GARMINTOKENS_BASE64", bootstrap)
    with pytest.raises(GarminSessionError):
        load_garmin_session()
    assert redis.writes == []


def test_bootstrap_write_failure_stops_startup(redis):
    redis.write_error = True
    with pytest.raises(GarminSessionError, match="Redis SET failed"):
        load_garmin_session()
    assert redis.value is None


def test_refresh_write_failure_surfaces_and_retries_before_rotating(redis, monkeypatch, caplog):
    garmin = load_garmin_session()
    original = redis.value
    renewed = session("renewed")
    post = mock_refresh(monkeypatch, renewed)
    redis.write_error = True
    with caplog.at_level(logging.DEBUG), pytest.raises(GarminSessionError, match="persistence failed"):
        garmin.client._refresh_session()
    assert redis.value == original
    assert garmin.client.di_refresh_token == "secret-refresh-renewed"
    assert "renewed tokens remain in memory" in caplog.text
    assert "secret-" not in caplog.text
    with pytest.raises(GarminSessionError, match="Redis SET failed"):
        garmin.client._refresh_session()
    assert post.call_count == 1

    redis.write_error = False
    # The pending state must be saved before any subsequent Garmin refresh.
    def next_refresh(*args, **kwargs):
        assert json.loads(redis.value) == json.loads(renewed)
        return Mock(ok=True, json=Mock(return_value={
            "access_token": json.loads(renewed)["di_token"],
            "refresh_token": "secret-refresh-next",
        }))

    post.side_effect = next_refresh
    garmin.client._refresh_session()
    assert json.loads(redis.value)["di_refresh_token"] == "secret-refresh-next"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_pending_session_saved_on_next_request_without_refresh(redis, monkeypatch, method):
    garmin = load_garmin_session()
    renewed = session("renewed")
    post = mock_refresh(monkeypatch, renewed)
    redis.write_error = True
    with pytest.raises(GarminSessionError, match="persistence failed"):
        garmin.client._refresh_session()
    assert not garmin.client._token_expires_soon()
    redis.write_error = False
    redis.writes.clear()

    def api_request(*args, **kwargs):
        # Persistence must succeed before the Garmin operation is sent.
        assert json.loads(redis.value) == json.loads(renewed)
        return Mock(status_code=200)

    request = Mock(side_effect=api_request)
    monkeypatch.setattr(garmin.client._api_session, "request", request)
    garmin.client.request(method, "connectapi", "/test")
    assert post.call_count == 1  # No additional refresh needed.
    assert redis.writes == [garmin.client.dumps()]
    assert request.call_count == 1
    assert request.call_args.args[0] == method

    # A successful save clears pending state: normal calls don't write again.
    garmin.client.request(method, "connectapi", "/test")
    assert len(redis.writes) == 1
    assert request.call_count == 2
    assert post.call_count == 1


def test_pending_save_failure_does_not_send_or_replay_garmin_operation(redis, monkeypatch):
    garmin = load_garmin_session()
    post = mock_refresh(monkeypatch, session("renewed"))
    redis.write_error = True
    with pytest.raises(GarminSessionError, match="persistence failed"):
        garmin.client._refresh_session()
    request = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(garmin.client._api_session, "request", request)
    with pytest.raises(GarminSessionError, match="Redis SET failed"):
        garmin.client.post("connectapi", "/first-operation", json={"value": 1})
    request.assert_not_called()
    assert post.call_count == 1

    redis.write_error = False
    garmin.client.post("connectapi", "/next-operation", json={"value": 2})
    request.assert_called_once()
    assert request.call_args.args[1].endswith("/next-operation")
    assert request.call_args.kwargs["json"] == {"value": 2}
    assert post.call_count == 1
    assert redis.value == garmin.client.dumps()


def test_login_refresh_write_failure_does_not_log_tokenstore(redis, monkeypatch, caplog):
    redis.value = session("expired", expired=True)
    redis.write_error = True
    mock_refresh(monkeypatch, session("renewed"))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
        init_api()
    assert "token details redacted" in caplog.text
    assert "secret-" not in caplog.text
    assert "signature" not in caplog.text


def test_failed_garmin_refresh_does_not_overwrite_redis(redis, monkeypatch):
    garmin = load_garmin_session()
    original = redis.value
    monkeypatch.setattr(Client, "_http_post", Mock(return_value=Mock(ok=False, status_code=400, text="invalid_grant")))
    redis.writes.clear()
    garmin.client._refresh_session()
    assert redis.value == original
    assert redis.writes == []


def test_garmin_refresh_error_body_is_redacted(redis, monkeypatch, caplog):
    garmin = load_garmin_session()
    monkeypatch.setattr(Client, "_http_post", Mock(return_value=Mock(
        ok=False, status_code=400, text="secret-refresh-bootstrap",
    )))
    with caplog.at_level(logging.DEBUG):
        garmin.client._refresh_session()
    assert "secret-" not in caplog.text
    assert "token details redacted" in caplog.text


def test_rejected_redis_session_never_uses_bootstrap(redis, monkeypatch, caplog):
    redis.value = session("rejected")
    monkeypatch.setattr(Garmin, "_load_profile_and_settings", Mock(
        side_effect=RuntimeError("secret-refresh-rejected"),
    ))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit):
        init_api()
    assert "login from Redis failed" in caplog.text
    assert "secret-" not in caplog.text
    assert redis.writes == []


def test_short_session_data_is_never_interpreted_as_a_path(redis, monkeypatch):
    redis.value = json.dumps({
        "di_token": "short", "di_refresh_token": "short", "di_client_id": "test",
    })
    file_load = Mock(side_effect=AssertionError("Session must not be a path"))
    monkeypatch.setattr(Client, "load", file_load)
    load_garmin_session()
    file_load.assert_not_called()


def test_expiry_log_describes_access_token_only(redis, caplog):
    garmin = load_garmin_session()
    with caplog.at_level(logging.INFO):
        _log_token_expiry(garmin)
    assert "Garmin access token expiry:" in caplog.text
    assert "refresh token valid until" not in caplog.text
    assert "secret-" not in caplog.text
