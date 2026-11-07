"""Проверка местных настроек без сети, записи файлов и показа секретов."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

NAMES = {"APP_ORIGIN", "MAX_BOT_TOKEN", "MAX_WEBHOOK_SECRET"}


def valid_origin(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and bool(url.hostname)
                and url.username is None and url.password is None
                and not url.path and not url.query and not url.fragment
                and url.port in (None, 443)
                and url.hostname not in ("localhost", "127.0.0.1", "::1")
                and re.fullmatch(r"[a-zA-Z0-9.-]+", url.hostname) is not None)
    except (ValueError, TypeError):
        return False


def inspect_config(env_file=None, environ=None):
    values = dict(os.environ if environ is None else environ)
    checks = []
    if env_file is not None:
        path = Path(env_file)
        try:
            info = path.lstat()
            regular = stat.S_ISREG(info.st_mode)
            checks.append({"check": "env_regular_file", "ok": regular})
            if not regular:
                return checks
            private = not (stat.S_IMODE(info.st_mode) & 0o077)
            checks.append({"check": "env_private_permissions", "ok": private})
            raw = path.read_text(encoding="utf-8") if info.st_size <= 16384 else None
            parsed = {}
            valid = raw is not None
            for line in (raw or "").splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                name, separator, value = line.partition("=")
                if not separator or name not in NAMES or name in parsed:
                    valid = False
                    break
                parsed[name] = value
            checks.append({"check": "env_file_format", "ok": valid})
            if not valid:
                return checks
            values.update(parsed)
        except (OSError, UnicodeError):
            checks.append({"check": "env_file_readable", "ok": False})
            return checks
    token = values.get("MAX_BOT_TOKEN", "")
    secret = values.get("MAX_WEBHOOK_SECRET", "")
    checks.extend([
        {"check": "https_origin_port_443", "ok": valid_origin(values.get("APP_ORIGIN", ""))},
        {"check": "bot_token_present", "ok": bool(token) and not any(c.isspace() for c in token)},
        {"check": "webhook_secret_format", "ok": re.fullmatch(r"[a-zA-Z0-9_-]{5,256}", secret) is not None},
    ])
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Проверить локальный файл без выполнения его содержимого")
    args = parser.parse_args()
    checks = inspect_config(args.env_file)
    ok = all(item["ok"] for item in checks)
    print(json.dumps({"local_config_ok": ok, "checks": checks,
                      "network_checked": False, "bot_live_confirmed": False}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
