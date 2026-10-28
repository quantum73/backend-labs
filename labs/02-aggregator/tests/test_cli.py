"""CLI проверяется подпроцессом: сигналы, коды выхода и stdout видны только снаружи процесса.

Это медленные тесты (секунды), поэтому их немного: логика остановки покрыта в test_pipeline.py.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aggregator_app.cli import EXIT_FAILURE, EXIT_OK, _exit_code, _stop_on_signals
from aggregator_app.pipeline import RunSummary, ShutdownMode

TIMEOUT_S = 30


def _start(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    # cwd=tmp_path: .env из репозитория не подхватится, процесс видит только переданное окружение
    with (tmp_path / "stderr.log").open("w") as stderr:  # у дочернего процесса своя копия дескриптора
        return subprocess.Popen(  # noqa: S603 — аргументы задаёт сам тест
            [sys.executable, "-m", "aggregator_app.cli", *args],
            cwd=tmp_path,
            env=os.environ | (env or {}),
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        )


def _events(tmp_path: Path) -> dict[str, dict[str, Any]]:
    lines = (tmp_path / "stderr.log").read_text().splitlines()
    return {event["event"]: event for event in map(json.loads, lines)}


def test_run_writes_results_to_stdout_and_summary_to_stderr(tmp_path):
    proc = _start(tmp_path, "--skus", "20", "--seed", "1")
    stdout, _ = proc.communicate(timeout=TIMEOUT_S)
    assert proc.returncode == EXIT_OK

    results = [json.loads(line) for line in stdout.splitlines()]
    assert sorted(result["sku"] for result in results) == [f"SKU-{n:05d}" for n in range(20)]

    finished = _events(tmp_path)["run_finished"]
    assert finished["shutdown"] == "completed"
    assert finished["processed"] == 20
    assert finished["max_in_flight"] <= 20
    assert {"throughput_sku_per_s", "loop_lag_p99_ms", "loop_lag_max_ms"} <= finished.keys()


def test_sigterm_stops_gracefully_with_whole_lines(tmp_path):
    proc = _start(tmp_path, "--skus", "2000", "--seed", "1")
    assert proc.stdout is not None

    first_line = proc.stdout.readline()
    proc.send_signal(signal.SIGTERM)
    rest, _ = proc.communicate(timeout=TIMEOUT_S)
    assert proc.returncode == EXIT_OK
    lines = [first_line, *rest.splitlines()]

    assert all(json.loads(line) for line in lines)  # каждая строка, включая последнюю, — целый JSON

    finished = _events(tmp_path)["run_finished"]
    assert finished["shutdown"] == "graceful"
    assert finished["processed"] == len(lines)
    assert finished["not_started"] > 0
    assert finished["enqueued"] == finished["processed"] + finished["not_started"]


def test_invalid_config_exits_with_2(tmp_path):
    proc = _start(tmp_path, "--skus", "5", env={"APP_WORKERS": "0"})
    stdout, _ = proc.communicate(timeout=TIMEOUT_S)
    assert proc.returncode == 2
    assert stdout == ""

    [error] = _events(tmp_path)["config_invalid"]["errors"]
    assert error["field"] == "workers"


def test_closed_stdout_stops_without_traceback(tmp_path):
    proc = _start(tmp_path, "--skus", "2000", "--seed", "1")
    assert proc.stdout is not None

    for _ in range(3):
        proc.stdout.readline()
    proc.stdout.close()  # как `| head -3`
    proc.wait(timeout=TIMEOUT_S)

    stderr = (tmp_path / "stderr.log").read_text()
    assert "Traceback" not in stderr
    assert "Exception ignored" not in stderr
    assert proc.returncode == EXIT_FAILURE  # вывод неполный

    finished = _events(tmp_path)["run_finished"]
    assert finished["output_closed"] is True
    assert finished["shutdown"] == "graceful"


@pytest.mark.asyncio
async def test_first_signal_stops_second_forces():
    stop, force = asyncio.Event(), asyncio.Event()

    with _stop_on_signals(stop, force):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=1)
        assert not force.is_set()

        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(force.wait(), timeout=1)

    loop = asyncio.get_running_loop()
    assert not loop.remove_signal_handler(signal.SIGTERM)  # обработчики сняты
    assert not loop.remove_signal_handler(signal.SIGINT)


def _summary(shutdown: ShutdownMode, *, errors: int = 0, contract_violations: int = 0) -> RunSummary:
    cancelled = 1 if shutdown is ShutdownMode.FORCED else 0
    return RunSummary(
        enqueued=1 + errors + contract_violations + cancelled,
        processed=1,
        not_started=0,
        cancelled=cancelled,
        errors=errors,
        contract_violations=contract_violations,
        statuses={},
        elapsed_s=1.0,
        shutdown=shutdown,
    )


@pytest.mark.parametrize(
    ("summary", "output_closed", "expected"),
    [
        pytest.param(_summary(ShutdownMode.COMPLETED), False, EXIT_OK, id="completed"),
        pytest.param(_summary(ShutdownMode.GRACEFUL), False, EXIT_OK, id="graceful"),
        pytest.param(_summary(ShutdownMode.FORCED), False, EXIT_FAILURE, id="forced"),
        pytest.param(_summary(ShutdownMode.COMPLETED, errors=1), False, EXIT_FAILURE, id="errors"),
        pytest.param(_summary(ShutdownMode.COMPLETED, contract_violations=1), False, EXIT_FAILURE, id="violations"),
        pytest.param(_summary(ShutdownMode.GRACEFUL), True, EXIT_FAILURE, id="output-closed"),
    ],
)
def test_exit_code(summary: RunSummary, output_closed: bool, expected: int):
    assert _exit_code(summary, output_closed=output_closed) == expected
