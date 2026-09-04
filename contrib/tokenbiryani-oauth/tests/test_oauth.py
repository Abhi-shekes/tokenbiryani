"""What is left of this package after `oauth` moved into core: a shim.

The behaviour tests moved with the code — see `tests/test_oauth_flow.py` and the
provider tests in the main suite. What matters here is the promise this package now
makes: existing imports still resolve, existing configuration still works, and
nothing tries to register an account type core already owns.
"""

from __future__ import annotations

import json
import os
import sys
import warnings

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "src"))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tokenbiryani.config import AccountConfig  # noqa: E402
from tokenbiryani.providers.anthropic_api import (  # noqa: E402
    available_types,
    build_upstream,
    register_upstream,
)


def write_credentials(tmp_path, token="sk-ant-oat01-abcdefghijklmnop", expires_in=3600):
    import time

    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": int((time.time() + expires_in) * 1000),
        }
    }))
    return str(path)


def test_the_account_type_is_built_in_now():
    assert "oauth" in available_types()


def test_registering_it_as_a_plugin_is_refused():
    """The reason the entry point had to go: a plugin may not shadow a built-in."""
    with pytest.raises(ValueError, match="built-in account type"):
        register_upstream("oauth", lambda config: None)


def test_the_old_imports_still_resolve():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import tokenbiryani_oauth

    from tokenbiryani.providers.oauth import OAuthUpstream

    assert tokenbiryani_oauth.OAuthUpstream is OAuthUpstream
    for name in ("CredentialError", "CredentialsFile", "EnvToken", "StaticToken",
                 "build_source"):
        assert hasattr(tokenbiryani_oauth, name)


def test_importing_it_warns_that_it_is_deprecated():
    for module in [m for m in list(sys.modules) if m.startswith("tokenbiryani_oauth")]:
        del sys.modules[module]
    with pytest.warns(DeprecationWarning, match="built into tokenbiryani"):
        import tokenbiryani_oauth  # noqa: F401


def test_a_credentials_file_config_still_works_against_core(tmp_path):
    """The compatibility promise: this config worked before, and must still."""
    path = write_credentials(tmp_path)
    upstream = build_upstream(AccountConfig(
        id="sub", type="oauth", options={"credentials_path": path},
    ))
    assert upstream.auth_headers()["authorization"].startswith("Bearer sk-ant-oat01-")
    assert "credentials file" in upstream.describe_source()


def test_a_static_token_config_still_works_against_core():
    upstream = build_upstream(AccountConfig(
        id="sub", type="oauth", options={"access_token": "tok-123"},
    ))
    assert upstream.auth_headers()["authorization"] == "Bearer tok-123"


def test_an_env_token_config_still_works_against_core(monkeypatch):
    monkeypatch.setenv("SUB_TOKEN", "tok-from-env")
    upstream = build_upstream(AccountConfig(
        id="sub", type="oauth", options={"token_env": "SUB_TOKEN"},
    ))
    assert upstream.auth_headers()["authorization"] == "Bearer tok-from-env"


def test_an_expired_credentials_file_says_what_to_do(tmp_path):
    from tokenbiryani.providers.oauth_credentials import CredentialError

    path = write_credentials(tmp_path, expires_in=-10)
    upstream = build_upstream(AccountConfig(
        id="sub", type="oauth", options={"credentials_path": path},
    ))
    with pytest.raises(CredentialError, match="expired"):
        upstream.auth_headers()


def test_a_configured_source_beats_a_gateway_session():
    """Explicit config wins, so an upgrade cannot silently change where a token comes from."""
    upstream = build_upstream(
        AccountConfig(id="sub", type="oauth", options={"access_token": "from-config"}),
        lambda: "from-gateway",
    )
    assert upstream.token() == "from-config"


def test_the_check_command_still_runs(tmp_path, capsys):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from tokenbiryani_oauth.check import main as check_main

    path = write_credentials(tmp_path)
    assert check_main(["--path", path]) == 0
    out = capsys.readouterr().out
    assert "claudeAiOauth.accessToken" in out
    assert "sk-ant-oat01-abcdefghijklmnop" not in out, "the token must stay redacted"
