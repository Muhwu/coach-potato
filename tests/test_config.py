import pytest

from server.config import ConfigError, load_config


def write_env(tmp_path, content):
    (tmp_path / ".env").write_text(content, encoding="utf-8")


def test_loads_api_key_from_env_file(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=RGAPI-test-key\nACCOUNTS=Foo#BAR\n")
    cfg = load_config(tmp_path)
    assert cfg.riot_api_key == "RGAPI-test-key"


def test_env_parser_ignores_comments_blanks_and_strips_quotes(tmp_path):
    write_env(tmp_path, "# comment\n\nRIOT_API_KEY=\"RGAPI-quoted\"\nACCOUNTS=Foo#BAR\nOTHER=x\n")
    cfg = load_config(tmp_path)
    assert cfg.riot_api_key == "RGAPI-quoted"


def test_accounts_parsed_from_env_file(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\nACCOUNTS=Foo#BAR, Baz#EUW\n")
    cfg = load_config(tmp_path)
    assert cfg.accounts == [("Foo", "BAR"), ("Baz", "EUW")]


def test_any_number_of_accounts(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\nACCOUNTS=A#EUW,B#EUW,C#NA1,D#KR\n")
    cfg = load_config(tmp_path)
    assert len(cfg.accounts) == 4
    assert cfg.accounts[3] == ("D", "KR")


def test_missing_accounts_raises_config_error(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\n")
    with pytest.raises(ConfigError, match="ACCOUNTS"):
        load_config(tmp_path)


def test_account_names_with_spaces_and_unicode(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\nACCOUNTS=Ünï côdé#EUW\n")
    cfg = load_config(tmp_path)
    assert cfg.accounts == [("Ünï côdé", "EUW")]


def test_db_path_defaults_under_root_data_dir(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\nACCOUNTS=Foo#BAR\n")
    cfg = load_config(tmp_path)
    assert cfg.db_path == tmp_path / "data" / "lol.sqlite"


def test_missing_key_raises_config_error(tmp_path):
    write_env(tmp_path, "OTHER=x\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_missing_env_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_platform_defaults_to_euw1(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\nACCOUNTS=Foo#BAR\n")
    assert load_config(tmp_path).platform == "euw1"


def test_platform_parsed_and_lowercased(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\nACCOUNTS=Foo#NA1\nPLATFORM=NA1\n")
    assert load_config(tmp_path).platform == "na1"


def test_unknown_platform_raises_config_error(tmp_path):
    write_env(tmp_path, "RIOT_API_KEY=k\nACCOUNTS=Foo#BAR\nPLATFORM=moon1\n")
    with pytest.raises(ConfigError, match="PLATFORM"):
        load_config(tmp_path)


def test_default_db_path_env_var_wins(tmp_path, monkeypatch):
    from server import config
    monkeypatch.setenv("LOL_DB_PATH", str(tmp_path / "x.sqlite"))
    assert config.default_db_path() == tmp_path / "x.sqlite"


def test_default_db_path_dev_mode(monkeypatch):
    from server import config
    monkeypatch.delenv("LOL_DB_PATH", raising=False)
    assert config.default_db_path() == config.PROJECT_ROOT / "data" / "lol.sqlite"


def test_default_db_path_frozen_linux(monkeypatch, tmp_path):
    import sys
    from server import config
    monkeypatch.delenv("LOL_DB_PATH", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = config.default_db_path()
    assert path == tmp_path / "coach-potato" / "lol.sqlite"


def test_resolve_settings_from_db(tmp_path, monkeypatch):
    from server import config, db
    monkeypatch.setattr(config, "ENV_FALLBACK_ROOT", tmp_path)  # no .env here
    conn = db.connect(tmp_path / "t.sqlite")
    empty = config.resolve_settings(conn)
    assert empty["configured"] is False
    assert empty["source"] is None
    db.set_settings(conn, {"riot_api_key": "RGAPI-x",
                           "accounts": '["Foo#BAR", "Baz#EUW"]',
                           "platform": "na1"})
    resolved = config.resolve_settings(conn)
    assert resolved["configured"] is True
    assert resolved["source"] == "db"
    assert resolved["riot_api_key"] == "RGAPI-x"
    assert resolved["accounts"] == ["Foo#BAR", "Baz#EUW"]
    assert resolved["platform"] == "na1"
    conn.close()


def test_resolve_settings_falls_back_to_env_file(tmp_path, monkeypatch):
    from server import config, db
    write_env(tmp_path, "RIOT_API_KEY=RGAPI-env\nACCOUNTS=A#EUW\nPLATFORM=euw1\n")
    monkeypatch.setattr(config, "ENV_FALLBACK_ROOT", tmp_path)
    conn = db.connect(tmp_path / "t.sqlite")
    resolved = config.resolve_settings(conn)
    assert resolved["configured"] is True
    assert resolved["source"] == "env"
    assert resolved["riot_api_key"] == "RGAPI-env"
    assert resolved["accounts"] == ["A#EUW"]
    conn.close()
