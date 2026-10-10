import logging
from datetime import datetime, timedelta

import pytest

from utils import logger as logger_module


@pytest.fixture
def file_logging(monkeypatch):
    monkeypatch.setattr(logger_module, "_file_handler", None)
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def _flush(root):
    for handler in root.handlers:
        handler.flush()


def test_file_logging_mirrors_console_lines_into_dated_file(tmp_path, file_logging):
    path = logger_module.enable_file_logging(tmp_path, "run-x")
    assert path is not None and path.startswith(str(tmp_path))
    logger_module.get_logger("stt").error("ElevenLabs STT error: ReadTimeout (status=None)")
    assert logger_module.enable_file_logging(tmp_path, "run-x") == path  # idempotent
    _flush(file_logging)
    text = open(path, encoding="utf-8").read()
    assert "=== run run-x started" in text
    assert "ElevenLabs STT error: ReadTimeout" in text


def test_file_lines_redact_provider_headers_bodies_and_configured_secrets(tmp_path, file_logging):
    secret = "sk-live-SYNTHETIC-0123456789"
    path = logger_module.enable_file_logging(tmp_path, "run-x", secrets=(secret, "", "short"))
    log = logger_module.get_logger("stt")
    log.error(
        "provider failed: headers: {'set-cookie': 'session=SYNTHETIC-COOKIE', 'x-request-id': 'r1'}, "
        "status_code: 503, body: {'detail': 'SYNTHETIC-BODY-TOKEN'}"
    )
    log.error("Groq STT error: key %s rejected", secret)
    _flush(file_logging)
    text = open(path, encoding="utf-8").read()
    assert "SYNTHETIC-COOKIE" not in text
    assert "SYNTHETIC-BODY-TOKEN" not in text
    assert secret not in text
    assert "status_code: 503" in text
    assert "headers: <redacted>" in text and "body: <redacted>" in text


def test_file_switches_at_local_midnight_and_prunes_old_logs(tmp_path, file_logging, monkeypatch):
    old = tmp_path / f"app_{(datetime.now() - timedelta(days=40)).strftime('%Y%m%d')}.log"
    recent = tmp_path / f"app_{(datetime.now() - timedelta(days=3)).strftime('%Y%m%d')}.log"
    old.write_text("old", encoding="utf-8")
    recent.write_text("recent", encoding="utf-8")
    # init, "run started" line, "before midnight" line -> 20261010; then the date changes
    days = iter(["20261010", "20261010", "20261010", "20261011"])
    monkeypatch.setattr(logger_module._DailyFileHandler, "_today", staticmethod(lambda: next(days, "20261011")))
    logger_module.enable_file_logging(tmp_path, "run-x")
    log = logger_module.get_logger("main")
    log.info("before midnight")
    log.info("after midnight")
    _flush(file_logging)
    assert not old.exists() and recent.exists()
    assert "before midnight" in (tmp_path / "app_20261010.log").read_text(encoding="utf-8")
    after = (tmp_path / "app_20261011.log").read_text(encoding="utf-8")
    assert "after midnight" in after and "before midnight" not in after


def test_unwritable_log_dir_warns_and_returns_none(tmp_path, file_logging, caplog):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert logger_module.enable_file_logging(blocker / "logs", "run-x") is None
    assert "App log file disabled" in caplog.text


def test_file_that_cannot_be_opened_warns_and_leaves_no_handler(tmp_path, file_logging, caplog, monkeypatch):
    def refuse(self):
        raise PermissionError("locked")

    monkeypatch.setattr(logger_module._DailyFileHandler, "_open", refuse)
    before = list(file_logging.handlers)
    with caplog.at_level(logging.WARNING):
        assert logger_module.enable_file_logging(tmp_path, "run-x") is None
    assert "App log file disabled: PermissionError" in caplog.text
    assert file_logging.handlers == before
    logger_module.get_logger("main").info("pipeline keeps logging to the console")


def test_failed_midnight_reopen_never_raises_into_the_caller(tmp_path, file_logging, monkeypatch):
    days = iter(["20261010", "20261010", "20261011"])
    monkeypatch.setattr(logger_module._DailyFileHandler, "_today", staticmethod(lambda: next(days, "20261011")))
    logger_module.enable_file_logging(tmp_path, "run-x")
    handler = logger_module._file_handler

    def refuse(self):
        raise PermissionError("locked")

    monkeypatch.setattr(logger_module._DailyFileHandler, "_open", refuse)
    monkeypatch.setattr(logging, "raiseExceptions", False)
    errors = []
    monkeypatch.setattr(handler, "handleError", lambda record: errors.append(record))
    logger_module.get_logger("stt").info("after midnight")  # must not raise
    assert len(errors) == 1


@pytest.mark.parametrize("run_kind, expect_enabled", [("test", False), ("live", True)])
def test_main_enables_file_logging_only_outside_tests(monkeypatch, run_kind, expect_enabled):
    from types import SimpleNamespace

    import main
    from utils.runtime_events import runtime_events

    called = []
    monkeypatch.setattr(runtime_events, "run_kind", run_kind)
    monkeypatch.setattr("utils.logger.enable_file_logging", lambda *a, **k: called.append(k))
    monkeypatch.setattr(main, "_parse_args", lambda: SimpleNamespace(
        calibrate_identity_roi=False, show_identity_roi=False, stt_only=False, listen=False))
    monkeypatch.setattr(main, "_validate_config", lambda *_: (_ for _ in ()).throw(SystemExit(2)))
    monkeypatch.setattr(main, "_export_chatgpt_bundle_on_shutdown", lambda **_: None)
    with pytest.raises(SystemExit):
        main.main()
    assert bool(called) is expect_enabled
    if called:
        assert "secrets" in called[0]
