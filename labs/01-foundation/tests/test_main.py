import json
import os
import subprocess
import sys

import pytest

from foundation_app.config import LOGGED_SETTINGS_FIELDS
from foundation_app.main import BAD_EXIT_CODE, GOOD_EXIT_CODE, main


def _read_single_event(capsys) -> dict:
    out, err = capsys.readouterr()
    assert not err
    lines = out.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


@pytest.mark.usefixtures("valid_env")
def test_main_logs_app_started(capsys):
    assert main() == GOOD_EXIT_CODE

    entry = _read_single_event(capsys)
    assert entry["event"] == "app_started"
    assert entry["level"] == "info"
    assert entry["run_id"]
    assert entry["settings"].keys() == LOGGED_SETTINGS_FIELDS
    assert entry["settings"]["env"] == "test"
    assert entry["settings"]["http_timeout_s"] == 14.0


@pytest.mark.usefixtures("valid_env")
def test_main_logs_config_invalid(monkeypatch, capsys):
    monkeypatch.setenv("APP_HTTP_TIMEOUT_S", "140.0")

    assert main() == BAD_EXIT_CODE

    entry = _read_single_event(capsys)
    assert entry["event"] == "config_invalid"
    assert entry["level"] == "error"
    assert "run_id" not in entry
    assert entry["errors"] == [
        {
            "field": "http_timeout_s",
            "type": "less_than_equal",
            "msg": "Input should be less than or equal to 30",
        },
    ]


@pytest.mark.parametrize(
    ("overrides", "exit_code"),
    [
        pytest.param({}, GOOD_EXIT_CODE, id="valid-json"),
        pytest.param({"APP_LOG_JSON": "false"}, GOOD_EXIT_CODE, id="valid-console"),
        pytest.param({"APP_HTTP_TIMEOUT_S": "140"}, BAD_EXIT_CODE, id="invalid-timeout"),
        pytest.param({"APP_DATABASE_HOST": "bad host"}, BAD_EXIT_CODE, id="invalid-dsn-host"),
        pytest.param({"APP_DATABASE_DRIVER": "mysql"}, BAD_EXIT_CODE, id="invalid-dsn-scheme"),
        pytest.param({"APP_DATABASE_PORT": "70000"}, BAD_EXIT_CODE, id="invalid-dsn-port"),
    ],
)
def test_main_never_prints_secrets(
    monkeypatch,
    capsys,
    secrets: tuple[str, ...],
    overrides: dict,
    exit_code: int,
):
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)

    assert main() == exit_code
    out, err = capsys.readouterr()
    assert out
    assert not [secret for secret in secrets if secret in out + err]


@pytest.mark.usefixtures("valid_env")
def test_main_reconfigures_logging_on_each_run(monkeypatch, capsys):
    monkeypatch.setenv("APP_HTTP_TIMEOUT_S", "140")
    assert main() == BAD_EXIT_CODE
    capsys.readouterr()

    monkeypatch.setenv("APP_HTTP_TIMEOUT_S", "14")
    assert main() == GOOD_EXIT_CODE

    assert _read_single_event(capsys)["event"] == "app_started"


@pytest.mark.usefixtures("valid_env")
def test_main_ignores_dotenv_in_cwd(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("APP_LOG_JSON=false\n")
    monkeypatch.chdir(tmp_path)

    assert main() == GOOD_EXIT_CODE

    assert _read_single_event(capsys)["event"] == "app_started"


@pytest.mark.usefixtures("valid_env")
@pytest.mark.parametrize(
    ("overrides", "exit_code"),
    [
        pytest.param({}, GOOD_EXIT_CODE, id="valid"),
        pytest.param({"APP_HTTP_TIMEOUT_S": "140"}, BAD_EXIT_CODE, id="invalid"),
    ],
)
def test_module_entrypoint_propagates_exit_code(tmp_path, overrides, exit_code):
    result = subprocess.run(
        [sys.executable, "-m", "foundation_app.main"],
        cwd=tmp_path,
        env=os.environ | overrides,
        capture_output=True,
        check=False,
    )
    assert result.returncode == exit_code
