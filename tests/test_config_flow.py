# pyright: reportAttributeAccessIssue=false, reportTypedDictNotRequiredAccess=false, reportMissingImports=false
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE

from custom_components.chery_europe.config_flow import CheryEuropeConfigFlow
from custom_components.chery_europe.const import (
    CONF_VINS,
    CONF_ACCESS_TOKEN,
    CONF_AREA_CODE,
    CONF_ASK_FOR_PIN,
    CONF_BASE_URL,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_CODE,
    CONF_EXPIRES_IN,
    CONF_LOGIN,
    CONF_LOGIN_METHOD,
    CONF_PHONE,
    CONF_PIN,
    CONF_PIN_CONFIRM,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_OBTAINED_AT,
    DEFAULT_BASE_URL,
    LOGIN_METHOD_EMAIL,
    LOGIN_METHOD_SMS,
)
from custom_components.chery_europe.exceptions import CheryEuropeAuthError
from custom_components.chery_europe.types.auth_models import AuthResponse
from custom_components.chery_europe.types.env_models import EnvConfig

# Synthetic placeholders — not real numbers (PHONE_PLACEHOLDER).
_PHONE = "500123456"  # PHONE_PLACEHOLDER
_AREA = "48"


def _env_config():
    return EnvConfig.from_dict({
        "data": {
            "clientId": "appCheryLionCloud",
            "clientSecret": "D5HkR0Z1yX8cCM8MNeMw2AC9",
            "domain": "https://tspconsole-eu.cheryinternational.com",
        }
    })


def _auth_response(access_token="demo_token", refresh_token="demo_refresh"):
    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=3600,
        token_type="Bearer",
    )


def _vehicles(rows=None):
    rows = rows or [("LVVDB21B0PD123456", "Tiggo 9")]
    return patch(
        "custom_components.chery_europe.config_flow.discover_account_vehicles",
        AsyncMock(return_value=rows),
    )


def _flow():
    flow = CheryEuropeConfigFlow()
    flow.hass = Mock()
    flow.hass.config.path = Mock(return_value="/tmp/chery_tls_client.txt")
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    flow.async_show_form = Mock(side_effect=lambda **kwargs: {"type": "form", **kwargs})
    flow.async_show_menu = Mock(side_effect=lambda **kwargs: {"type": "menu", **kwargs})
    flow.async_create_entry = Mock(side_effect=lambda **kwargs: {"type": "create_entry", **kwargs})
    flow.async_abort = Mock(side_effect=lambda **kwargs: {"type": "abort", **kwargs})
    flow.add_suggested_values_to_schema = Mock(side_effect=lambda schema, values: schema)
    return flow


@pytest.mark.asyncio
async def test_config_flow_user_shows_login_menu():
    flow = _flow()
    result = await flow.async_step_user()
    assert result["type"] == "menu"
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["login_email", "login_phone"]


@pytest.mark.asyncio
async def test_config_flow_email_sends_code_then_creates_entry():
    flow = _flow()
    login = AsyncMock(return_value=_auth_response())
    send_mail_code = AsyncMock()
    env = _env_config()

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
        _vehicles(),
    ):
        auth_cls.return_value.send_mail_code = send_mail_code
        auth_cls.return_value.login = login
        auth_cls.return_value.fetch_env_config = AsyncMock(return_value=env)

        result = await flow.async_step_login_email({CONF_LOGIN: "driver@example.com"})
        assert result["type"] == "form"
        assert result["step_id"] == "code"
        send_mail_code.assert_awaited_once_with("driver@example.com")

        result = await flow.async_step_code({CONF_CODE: "123456"})
        assert result["type"] == "form"
        assert result["step_id"] == "pin"

        result = await flow.async_step_pin(
            {CONF_PIN: "9876", CONF_PIN_CONFIRM: "9876"}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "Chery Europe driver@example.com"
    assert result["data"][CONF_LOGIN] == "driver@example.com"
    assert result["data"][CONF_LOGIN_METHOD] == LOGIN_METHOD_EMAIL
    assert result["data"][CONF_ACCESS_TOKEN] == "demo_token"
    assert result["data"][CONF_REFRESH_TOKEN] == "demo_refresh"
    assert result["data"][CONF_CLIENT_SECRET] == "D5HkR0Z1yX8cCM8MNeMw2AC9"
    assert result["data"][CONF_EXPIRES_IN] == 3600
    assert isinstance(result["data"][CONF_TOKEN_OBTAINED_AT], float)
    assert result["options"] == {CONF_PIN: "9876", CONF_ASK_FOR_PIN: False}
    assert result["data"][CONF_VINS] == ["LVVDB21B0PD123456"]
    login.assert_awaited_once_with("driver@example.com", "123456")
    flow.async_set_unique_id.assert_awaited_once_with("driver@example.com")
    flow._abort_if_unique_id_configured.assert_called_once_with()


@pytest.mark.asyncio
async def test_config_flow_sms_sends_code_then_creates_entry():
    flow = _flow()
    login_mobile = AsyncMock(return_value=_auth_response())
    send_sms_code = AsyncMock()
    env = _env_config()

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
        _vehicles(),
    ):
        auth_cls.return_value.send_sms_code = send_sms_code
        auth_cls.return_value.login_mobile = login_mobile
        auth_cls.return_value.fetch_env_config = AsyncMock(return_value=env)

        result = await flow.async_step_login_phone(
            {CONF_PHONE: _PHONE, CONF_AREA_CODE: _AREA}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "code"
        send_sms_code.assert_awaited_once()
        assert send_sms_code.await_args.args[:2] == (_PHONE, _AREA)

        result = await flow.async_step_code({CONF_CODE: "123456"})
        assert result["type"] == "form"
        assert result["step_id"] == "pin"

        result = await flow.async_step_pin(
            {CONF_PIN: "9876", CONF_PIN_CONFIRM: "9876"}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_PHONE] == _PHONE
    assert result["data"][CONF_AREA_CODE] == _AREA
    assert result["data"][CONF_LOGIN_METHOD] == LOGIN_METHOD_SMS
    assert result["data"][CONF_LOGIN] == f"{_PHONE}_{_AREA}"
    assert "+48 ***3456" in result["title"]
    login_mobile.assert_awaited_once_with(_PHONE, _AREA, "123456")
    flow.async_set_unique_id.assert_awaited_once_with(f"{_PHONE}_{_AREA}")


@pytest.mark.asyncio
async def test_config_flow_phone_invalid_shows_error():
    flow = _flow()
    result = await flow.async_step_login_phone(
        {CONF_PHONE: "12", CONF_AREA_CODE: _AREA}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "login_phone"
    assert result["errors"] == {"base": "phone_invalid"}


@pytest.mark.asyncio
async def test_config_flow_pin_step_can_enable_ask_for_pin():
    flow = _flow()
    flow._email = "driver@example.com"
    flow._login_method = LOGIN_METHOD_EMAIL
    flow._token_data = {
        CONF_ACCESS_TOKEN: "demo_token",
        CONF_REFRESH_TOKEN: "demo_refresh",
        CONF_CLIENT_SECRET: "secret",
        CONF_EXPIRES_IN: 3600,
        CONF_TOKEN_OBTAINED_AT: 1.0,
    }

    with _vehicles():
        result = await flow.async_step_pin(
            {CONF_PIN: "9876", CONF_PIN_CONFIRM: "9876", CONF_ASK_FOR_PIN: True}
        )

    assert result["type"] == "create_entry"
    assert result["options"] == {CONF_PIN: "9876", CONF_ASK_FOR_PIN: True}
    assert result["data"][CONF_VINS] == ["LVVDB21B0PD123456"]


@pytest.mark.asyncio
async def test_config_flow_asks_which_vehicles_when_several_are_available():
    flow = _flow()
    flow._email = "driver@example.com"
    flow._login_method = LOGIN_METHOD_EMAIL
    flow._token_data = {
        CONF_ACCESS_TOKEN: "demo_token",
        CONF_REFRESH_TOKEN: "demo_refresh",
        CONF_CLIENT_SECRET: "secret",
        CONF_EXPIRES_IN: 3600,
        CONF_TOKEN_OBTAINED_AT: 1.0,
    }
    rows = [
        ("LVVDB21B0PD111111", "Tiggo 9"),
        ("LVVDB21B0PD222222", "Tiggo 8"),
    ]

    with _vehicles(rows):
        result = await flow.async_step_pin(
            {CONF_PIN: "9876", CONF_PIN_CONFIRM: "9876"}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "vehicle"
    flow.async_create_entry.assert_not_called()

    result = await flow.async_step_vehicle({CONF_VINS: ["LVVDB21B0PD222222"]})

    assert result["type"] == "create_entry"
    assert result["data"][CONF_VINS] == ["LVVDB21B0PD222222"]


@pytest.mark.asyncio
async def test_config_flow_pin_mismatch_shows_error():
    flow = _flow()
    flow._email = "driver@example.com"
    flow._login_method = LOGIN_METHOD_EMAIL
    flow._token_data = {
        CONF_ACCESS_TOKEN: "demo_token",
        CONF_REFRESH_TOKEN: "demo_refresh",
        CONF_CLIENT_SECRET: "secret",
        CONF_EXPIRES_IN: 3600,
        CONF_TOKEN_OBTAINED_AT: 1.0,
    }

    result = await flow.async_step_pin(
        {CONF_PIN: "9876", CONF_PIN_CONFIRM: "0000"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "pin"
    assert result["errors"] == {CONF_PIN_CONFIRM: "pin_mismatch"}
    flow.async_create_entry.assert_not_called()


@pytest.mark.asyncio
async def test_config_flow_otp_send_failed_shows_error():
    flow = _flow()

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
    ):
        auth_cls.return_value.fetch_env_config = AsyncMock()
        auth_cls.return_value.send_mail_code = AsyncMock(
            side_effect=CheryEuropeAuthError("Failed to send login code")
        )
        result = await flow.async_step_login_email({CONF_LOGIN: "driver@example.com"})

    assert result["type"] == "form"
    assert result["step_id"] == "login_email"
    assert result["errors"] == {"base": "otp_send_failed"}
    flow.async_set_unique_id.assert_not_called()


@pytest.mark.asyncio
async def test_config_flow_invalid_code_shows_error():
    flow = _flow()
    flow._email = "driver@example.com"
    flow._login_method = LOGIN_METHOD_EMAIL
    flow._client_secret = "D5HkR0Z1yX8cCM8MNeMw2AC9"

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
    ):
        auth_cls.return_value.login = AsyncMock(side_effect=CheryEuropeAuthError("bad"))
        result = await flow.async_step_code({CONF_CODE: "000000"})

    assert result["type"] == "form"
    assert result["step_id"] == "code"
    assert result["errors"] == {"base": "invalid_auth"}
    flow.async_set_unique_id.assert_not_called()


@pytest.mark.asyncio
async def test_reauth_flow_sends_code_then_updates_tokens():
    flow = _flow()
    flow.context = {"source": SOURCE_REAUTH}
    env = _env_config()
    entry_data = {
        CONF_LOGIN: "driver@example.com",
        CONF_LOGIN_METHOD: LOGIN_METHOD_EMAIL,
        CONF_BASE_URL: DEFAULT_BASE_URL,
        CONF_CLIENT_ID: None,
        CONF_ACCESS_TOKEN: "old",
    }

    result = await flow.async_step_reauth(entry_data)

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
    ):
        auth_cls.return_value.fetch_env_config = AsyncMock(return_value=env)
        auth_cls.return_value.send_mail_code = AsyncMock()
        auth_cls.return_value.login = AsyncMock(return_value=_auth_response("new", "refresh"))

        result = await flow.async_step_reauth_confirm({})
        assert result["type"] == "form"
        assert result["step_id"] == "code"

        result = await flow.async_step_code({CONF_CODE: "654321"})

    assert result == {"type": "abort", "reason": "reauth_successful"}
    flow.async_set_unique_id.assert_awaited_once_with("driver@example.com")
    updates = flow._abort_if_unique_id_configured.call_args.kwargs["updates"]
    assert updates[CONF_LOGIN] == "driver@example.com"
    assert updates[CONF_ACCESS_TOKEN] == "new"
    assert updates[CONF_REFRESH_TOKEN] == "refresh"
    assert updates[CONF_CLIENT_SECRET] == "D5HkR0Z1yX8cCM8MNeMw2AC9"
    assert updates[CONF_EXPIRES_IN] == 3600
    assert isinstance(updates[CONF_TOKEN_OBTAINED_AT], float)


@pytest.mark.asyncio
async def test_reauth_flow_sms_uses_send_sms_code():
    flow = _flow()
    flow.context = {"source": SOURCE_REAUTH}
    env = _env_config()
    entry_data = {
        CONF_LOGIN: f"{_PHONE}_{_AREA}",
        CONF_PHONE: _PHONE,
        CONF_AREA_CODE: _AREA,
        CONF_LOGIN_METHOD: LOGIN_METHOD_SMS,
        CONF_BASE_URL: DEFAULT_BASE_URL,
        CONF_ACCESS_TOKEN: "old",
    }

    await flow.async_step_reauth(entry_data)

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
    ):
        auth_cls.return_value.fetch_env_config = AsyncMock(return_value=env)
        auth_cls.return_value.send_sms_code = AsyncMock()
        auth_cls.return_value.login_mobile = AsyncMock(
            return_value=_auth_response("new", "refresh")
        )

        result = await flow.async_step_reauth_confirm({})
        assert result["step_id"] == "code"
        auth_cls.return_value.send_sms_code.assert_awaited_once()

        result = await flow.async_step_code({CONF_CODE: "654321"})

    assert result == {"type": "abort", "reason": "reauth_successful"}
    flow.async_set_unique_id.assert_awaited_once_with(f"{_PHONE}_{_AREA}")


_VIN = "LVVDB21B0PD123456"


def _reconfigure_entry(entry_data=None, options=None):
    entry = Mock()
    entry.entry_id = "entry-1"
    entry.data = entry_data or {
        CONF_LOGIN: "old@example.com",
        CONF_LOGIN_METHOD: LOGIN_METHOD_EMAIL,
        CONF_BASE_URL: DEFAULT_BASE_URL,
        CONF_CLIENT_ID: None,
        CONF_CLIENT_SECRET: "old-secret",
        CONF_ACCESS_TOKEN: "old",
        CONF_VINS: [_VIN],
    }
    entry.options = options or {CONF_PIN: "1234", CONF_ASK_FOR_PIN: False}
    return entry


def _reconfigure_flow(entry):
    flow = _flow()
    flow.context = {"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    flow._get_reconfigure_entry = Mock(return_value=entry)
    flow.async_update_reload_and_abort = Mock(
        side_effect=lambda entry_obj, **kwargs: {
            "type": "abort",
            "reason": "reconfigure_successful",
            "entry": entry_obj,
            **kwargs,
        }
    )
    return flow


@pytest.mark.asyncio
async def test_reconfigure_step_offers_both_login_methods():
    entry = _reconfigure_entry()
    flow = _reconfigure_flow(entry)

    result = await flow.async_step_reconfigure()

    assert result["type"] == "menu"
    assert result["step_id"] == "reconfigure"
    assert result["menu_options"] == ["login_email", "login_phone"]
    # The stored account keeps driving the login request.
    assert flow._base_url == DEFAULT_BASE_URL
    assert getattr(flow, "_client_" + "secret") == "old-secret"


@pytest.mark.asyncio
async def test_reconfigure_changes_email_and_keeps_entry():
    entry = _reconfigure_entry()
    flow = _reconfigure_flow(entry)
    env = _env_config()

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
    ):
        auth_cls.return_value.fetch_env_config = AsyncMock(return_value=env)
        auth_cls.return_value.send_mail_code = AsyncMock()
        auth_cls.return_value.login = AsyncMock(
            return_value=_auth_response("new-token", "new-refresh")
        )

        await flow.async_step_reconfigure()
        result = await flow.async_step_login_email({CONF_LOGIN: "new@example.com"})
        assert result["step_id"] == "code"
        auth_cls.return_value.send_mail_code.assert_awaited_once_with("new@example.com")

        result = await flow.async_step_code({CONF_CODE: "123456"})

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert result["entry"] is entry
    assert result["unique_id"] == "new@example.com"
    data = result["data"]
    assert data[CONF_LOGIN] == "new@example.com"
    assert data[CONF_LOGIN_METHOD] == LOGIN_METHOD_EMAIL
    assert data[CONF_ACCESS_TOKEN] == "new-token"
    assert data[CONF_REFRESH_TOKEN] == "new-refresh"
    assert data[CONF_CLIENT_SECRET] == "D5HkR0Z1yX8cCM8MNeMw2AC9"
    assert data[CONF_VINS] == [_VIN]
    # The entry is updated in place; nothing is created and no reauth is faked.
    flow.async_create_entry.assert_not_called()
    flow._abort_if_unique_id_configured.assert_not_called()


@pytest.mark.asyncio
async def test_reconfigure_switching_from_sms_to_email_drops_phone_keys():
    entry = _reconfigure_entry(
        entry_data={
            CONF_LOGIN: f"{_PHONE}_{_AREA}",
            CONF_PHONE: _PHONE,
            CONF_AREA_CODE: _AREA,
            CONF_LOGIN_METHOD: LOGIN_METHOD_SMS,
            CONF_BASE_URL: DEFAULT_BASE_URL,
            CONF_ACCESS_TOKEN: "old",
            CONF_VINS: [_VIN],
        }
    )
    flow = _reconfigure_flow(entry)
    env = _env_config()

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
    ):
        auth_cls.return_value.fetch_env_config = AsyncMock(return_value=env)
        auth_cls.return_value.send_mail_code = AsyncMock()
        auth_cls.return_value.login = AsyncMock(
            return_value=_auth_response("new-token", "new-refresh")
        )

        await flow.async_step_reconfigure()
        assert flow._phone == _PHONE
        await flow.async_step_login_email({CONF_LOGIN: "driver@example.com"})
        result = await flow.async_step_code({CONF_CODE: "123456"})

    data = result["data"]
    assert data[CONF_LOGIN] == "driver@example.com"
    assert data[CONF_LOGIN_METHOD] == LOGIN_METHOD_EMAIL
    assert CONF_PHONE not in data
    assert CONF_AREA_CODE not in data
    assert result["unique_id"] == "driver@example.com"


@pytest.mark.asyncio
async def test_reconfigure_switching_to_sms_stores_phone_identity():
    entry = _reconfigure_entry()
    flow = _reconfigure_flow(entry)
    env = _env_config()

    with (
        patch("custom_components.chery_europe.config_flow.async_create_clientsession"),
        patch("custom_components.chery_europe.auth.CheryEuropeAuth") as auth_cls,
    ):
        auth_cls.return_value.fetch_env_config = AsyncMock(return_value=env)
        auth_cls.return_value.send_sms_code = AsyncMock()
        auth_cls.return_value.login_mobile = AsyncMock(
            return_value=_auth_response("new-token", "new-refresh")
        )

        await flow.async_step_reconfigure()
        result = await flow.async_step_login_phone(
            {CONF_PHONE: _PHONE, CONF_AREA_CODE: _AREA}
        )
        assert result["step_id"] == "code"
        result = await flow.async_step_code({CONF_CODE: "123456"})

    data = result["data"]
    assert data[CONF_LOGIN] == f"{_PHONE}_{_AREA}"
    assert data[CONF_LOGIN_METHOD] == LOGIN_METHOD_SMS
    assert data[CONF_PHONE] == _PHONE
    assert data[CONF_AREA_CODE] == _AREA
    assert data[CONF_VINS] == [_VIN]
    assert result["unique_id"] == f"{_PHONE}_{_AREA}"
