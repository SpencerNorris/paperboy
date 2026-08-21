import logging

from paperboy.logging_setup import configure_logging, register_secret


def test_secret_is_masked(tmp_path, capsys):
    logf = tmp_path / "p.log"
    configure_logging(logf, console=True)
    register_secret("hunter2SECRET")
    logging.getLogger("paperboy").warning("session=%s", "hunter2SECRET")
    assert "hunter2SECRET" not in logf.read_text()
    assert "***" in logf.read_text()


def test_unregistered_text_passes_through(tmp_path):
    logf = tmp_path / "p2.log"
    configure_logging(logf, console=False)
    logging.getLogger("paperboy").info("resolved target=%s", "tg:channel:5")
    assert "tg:channel:5" in logf.read_text()


def test_multiple_secrets_all_masked(tmp_path):
    logf = tmp_path / "p3.log"
    configure_logging(logf, console=False)
    register_secret("apihashvalue")
    register_secret("sessionstringvalue")
    logging.getLogger("paperboy").warning(
        "auth api_hash=%s session=%s", "apihashvalue", "sessionstringvalue"
    )
    text = logf.read_text()
    assert "apihashvalue" not in text
    assert "sessionstringvalue" not in text
