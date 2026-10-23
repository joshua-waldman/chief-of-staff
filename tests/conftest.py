"""Keep synthetic tests independent of the owner's local app settings."""
import pytest
from cos import config
# Bind static asset paths before individual tests isolate configuration roots.
from cos import server  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    monkeypatch.setitem(config.DEFAULTS,'self_email','owner@example.com')
