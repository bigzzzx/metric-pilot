import pytest

from backend.provider_transport import client_options, normalize_connection


def test_direct_is_the_safe_default():
    assert normalize_connection(None, None) == ("direct", "")
    assert client_options({"kind": "openai-compatible"}) == {"trust_env": False}


def test_system_proxy_uses_process_proxy_configuration():
    provider = {"kind": "openai-compatible", "connection_mode": "system_proxy"}
    assert client_options(provider) == {"trust_env": True}


def test_custom_proxy_is_passed_only_to_remote_provider():
    provider = {
        "kind": "openai-compatible",
        "connection_mode": "custom_proxy",
        "proxy_url": "http://127.0.0.1:10808/",
    }
    assert client_options(provider) == {"trust_env": False, "proxy": "http://127.0.0.1:10808"}


def test_local_model_never_inherits_proxy():
    provider = {"kind": "ollama", "connection_mode": "custom_proxy", "proxy_url": "http://127.0.0.1:10808"}
    assert client_options(provider) == {"trust_env": False}


@pytest.mark.parametrize("proxy_url", ["", "socks5://127.0.0.1:10808", "http://"])
def test_invalid_custom_proxy_is_rejected(proxy_url):
    with pytest.raises(ValueError):
        normalize_connection("custom_proxy", proxy_url)
