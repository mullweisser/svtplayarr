import hmac
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)


def load_api_key():
    configured = os.environ.get("INDEXER_API_KEY", "").strip()
    if configured:
        return configured

    directory = Path(os.environ.get("CONFIG_DIR", "/config"))
    path = directory / "api_key"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError as exc:
        logger.error("Could not read indexer API key: %s", exc)

    generated = secrets.token_hex(16)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(generated + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        logger.info("Generated indexer API key in %s: %s", path, generated)
    except OSError as exc:
        logger.error("Could not store indexer API key: %s", exc)
    return generated


def api_key_matches(given, expected):
    if not expected or not given:
        return False
    return hmac.compare_digest(str(given).encode("utf-8"), str(expected).encode("utf-8"))
