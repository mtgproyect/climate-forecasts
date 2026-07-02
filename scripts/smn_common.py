from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

TOKEN_PAGE = "https://ws2.smn.gob.ar/pronostico"
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
}
TRANSIENT_HTTP_STATUS = {404, 408, 425, 429, 500, 502, 503, 504}


class TokenRejected(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: Path, payload: Any, *, minified: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if minified:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    else:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(serialized)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_token(session: requests.Session) -> str:
    response = session.get(
        TOKEN_PAGE,
        headers={**BASE_HEADERS, "Accept": "text/html,application/xhtml+xml"},
        timeout=30,
    )
    response.raise_for_status()
    patterns = [
        r"localStorage\.setItem\(\s*[\"']token[\"']\s*,\s*[\"']([^\"']+)[\"']\s*\)",
        r"localStorage\.setItem\(\s*`token`\s*,\s*`([^`]+)`\s*\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, response.text)
        if match:
            token = match.group(1).strip()
            if token.count(".") == 2:
                return token
    raise RuntimeError("No se pudo obtener el token temporal del SMN.")


def api_headers(token: str) -> dict[str, str]:
    return {
        **BASE_HEADERS,
        "Accept": "application/json",
        "Authorization": f"JWT {token}",
        "Origin": "https://ws2.smn.gob.ar",
        "Referer": "https://ws2.smn.gob.ar/",
    }


def legacy_headers() -> dict[str, str]:
    return {
        **BASE_HEADERS,
        "Accept": "application/json",
        "Origin": "https://www.smn.gob.ar",
        "Referer": "https://www.smn.gob.ar/",
    }


def response_json(response: requests.Response, description: str) -> Any:
    if response.status_code in {401, 403}:
        raise TokenRejected(f"El token fue rechazado al consultar {description}.")
    if not response.ok:
        raise ApiError(
            f"HTTP {response.status_code} al consultar {description}.",
            response.status_code,
        )
    try:
        return response.json()
    except ValueError as error:
        raise ApiError(
            f"{description} no devolvió JSON válido.", response.status_code
        ) from error


def is_transient_error(error: Exception) -> bool:
    if isinstance(error, TokenRejected):
        return True
    if isinstance(error, ApiError):
        return error.status_code in TRANSIENT_HTTP_STATUS
    return isinstance(error, requests.RequestException)


def request_with_retries(
    session: requests.Session,
    token: str,
    function: Callable[[requests.Session, str, dict[str, Any]], dict[str, Any]],
    target: dict[str, Any],
    *,
    max_http_attempts: int,
    retry_base_seconds: float,
) -> tuple[dict[str, Any], str, int]:
    current_token = token
    last_error: Exception | None = None
    for attempt in range(1, max_http_attempts + 1):
        try:
            try:
                return function(session, current_token, target), current_token, attempt
            except TokenRejected:
                current_token = get_token(session)
                return function(session, current_token, target), current_token, attempt
        except Exception as error:
            last_error = error
            if not is_transient_error(error) or attempt >= max_http_attempts:
                raise
            delay = retry_base_seconds * (2 ** (attempt - 1))
            status_code = error.status_code if isinstance(error, ApiError) else None
            print(
                f"  Reintento {attempt + 1}/{max_http_attempts} en {delay:.1f}s "
                f"(HTTP {status_code!r})"
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def error_record(error: Exception) -> dict[str, Any]:
    return {
        "type": type(error).__name__,
        "message": str(error),
        "status_code": error.status_code if isinstance(error, ApiError) else None,
    }
