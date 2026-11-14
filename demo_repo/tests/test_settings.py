import pytest

from acme_api import db
from acme_api.settings import SettingsError, load


def test_missing_settings_file_names_the_example(tmp_path):
    with pytest.raises(SettingsError, match="copy settings.example.toml"):
        load(tmp_path / "settings.toml")


def test_settings_missing_a_key_is_explained(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[app]\nname = "acme-api"\n')
    with pytest.raises(SettingsError, match="'database'"):
        load(path)


def test_database_url_dialects():
    assert db.dialect("sqlite:///acme.db") == "sqlite"
    assert db.dialect("postgresql://acme@db/acme") == "postgresql"
    with pytest.raises(db.UnsupportedDatabase):
        db.dialect("mysql://acme@db/acme")
