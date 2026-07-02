from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path
from typing import Any

import requests

from smn_common import (
    ApiError,
    api_headers,
    as_int,
    error_record,
    get_token,
    legacy_headers,
    load_json,
    request_with_retries,
    response_json,
    sha256_file,
    utc_now,
    write_json_atomic,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config/pronosticos.json"
CACHE_FILE = ROOT / "data/cache/pronosticos.json"
LOCAL_LEGACY_FILE = ROOT / "data/fuentes/pronosticos_historicos_antartida.json"
DOCS_DIR = ROOT / "docs"
FORECAST_URL = "https://ws1.smn.gob.ar/v1/forecast/location/{location_id}"
LEGACY_FORECAST_URL = "https://ws.smn.gob.ar/forecast/location/{location_id}"
LEGACY_FALLBACK_STATUS = {404, 500, 502, 503, 504}


def normalize_legacy_forecast(payload: Any, reference_id: int) -> dict[str, Any]:
    if not isinstance(payload, list) or not payload:
        raise ApiError("El endpoint histórico no devolvió pronósticos.")
    candidates = []
    for item in payload:
        if not isinstance(item, dict) or as_int(item.get("location_id")) != reference_id:
            continue
        if isinstance(item.get("forecast"), (dict, list)):
            candidates.append(item)
    if not candidates:
        raise ApiError(f"No existe un pronóstico histórico válido para {reference_id}.")
    selected = max(candidates, key=lambda item: (as_int(item.get("timestamp")) or 0, str(item.get("date_time") or "")))
    raw = selected["forecast"]
    if isinstance(raw, list):
        days = [item for item in raw if isinstance(item, dict)]
    else:
        days = [item for _, item in sorted(raw.items(), key=lambda pair: as_int(pair[0]) if as_int(pair[0]) is not None else 10**9) if isinstance(item, dict)]
    if not days or any(not day.get("date") for day in days):
        raise ApiError("El pronóstico histórico no contiene días válidos.")
    return {
        "source": "smn_legacy_forecast",
        "historical": True,
        "updated": selected.get("date_time"),
        "location": {"id": reference_id},
        "type": "legacy_location",
        "forecast": days,
        "legacy_metadata": {
            "_id": selected.get("_id"),
            "timestamp": selected.get("timestamp"),
            "date_time": selected.get("date_time"),
            "location_id": selected.get("location_id"),
        },
    }


def local_legacy_forecast(reference_id: int) -> dict[str, Any] | None:
    source = load_json(LOCAL_LEGACY_FILE)
    raw = source.get("records", {}).get(str(reference_id))
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    result = normalize_legacy_forecast(raw, reference_id)
    result["source"] = "smn_legacy_local_seed"
    result["legacy_metadata"]["seed_file"] = "data/fuentes/pronosticos_historicos_antartida.json"
    return result


def fetch_forecast(session: requests.Session, token: str, target: dict[str, Any]) -> dict[str, Any]:
    reference_id = int(target["forecast_reference_id"])
    try:
        response = session.get(
            FORECAST_URL.format(location_id=reference_id),
            headers=api_headers(token),
            timeout=30,
        )
        payload = response_json(response, f"el pronóstico moderno {reference_id}")
        if not isinstance(payload, dict):
            raise ApiError("El pronóstico no es un objeto JSON.")
        location = payload.get("location")
        forecast = payload.get("forecast")
        if not isinstance(location, dict) or as_int(location.get("id")) != reference_id:
            raise ApiError("El pronóstico devolvió una referencia distinta.")
        if not isinstance(forecast, list) or not forecast:
            raise ApiError("El pronóstico no contiene días válidos.")
        payload.setdefault("source", "smn_modern_forecast")
        payload.setdefault("historical", False)
        return payload
    except ApiError as error:
        if error.status_code not in LEGACY_FALLBACK_STATUS:
            raise
        print("  Endpoint moderno no disponible; se intenta el histórico.")
        try:
            response = session.get(
                LEGACY_FORECAST_URL.format(location_id=reference_id),
                headers=legacy_headers(),
                timeout=30,
            )
            return normalize_legacy_forecast(
                response_json(response, f"el pronóstico histórico {reference_id}"),
                reference_id,
            )
        except Exception:
            local = local_legacy_forecast(reference_id)
            if local is not None:
                print("  Se utiliza el respaldo histórico oficial local.")
                return local
            raise


def slim_record(reference_id: int, record: dict[str, Any], generated_at: str) -> dict[str, Any]:
    result = {
        "schema_version": 1,
        "generated_at": generated_at,
        "forecast_reference_id": reference_id,
        "status": record.get("status"),
        "fresh": record.get("status") == "success",
        "historical": bool(record.get("historical")),
        "data_source": record.get("data_source"),
        "fetched_at": record.get("fetched_at"),
        "payload": record.get("payload"),
    }
    if record.get("last_refresh_attempt_at") is not None:
        result["last_refresh_attempt_at"] = record["last_refresh_attempt_at"]
    if record.get("last_refresh_error") is not None:
        result["last_refresh_error"] = record["last_refresh_error"]
    return result


def publish(cache: dict[str, Any], expected: int, run: dict[str, Any]) -> None:
    records = cache.get("records", {})
    if len(records) != expected:
        raise RuntimeError(f"Se encontraron {len(records)} pronósticos; se esperaban {expected}.")
    generated_at = utc_now()
    forecast_dir = DOCS_DIR / "pronosticos"
    forecast_dir.mkdir(parents=True, exist_ok=True)
    combined = hashlib.sha256()
    stale_ids = []
    errors = 0
    for raw_id in sorted(records, key=int):
        reference_id = int(raw_id)
        record = records[raw_id]
        if record.get("status") not in {"success", "stale"}:
            errors += 1
        if record.get("status") == "stale":
            stale_ids.append(reference_id)
        path = forecast_dir / f"{reference_id}.json"
        write_json_atomic(path, slim_record(reference_id, record, generated_at), minified=True)
        combined.update(f"{reference_id}:{sha256_file(path)}\n".encode("utf-8"))
    fresh = expected - len(stale_ids) - errors
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "counts": {"forecast_references": expected, "fresh": fresh, "stale": len(stale_ids), "errors": errors},
        "stale": {"forecast_reference_ids": stale_ids},
        "files": {"forecasts": {"directory": "pronosticos", "count": expected, "combined_sha256": combined.hexdigest()}},
        "validation": {"expected": expected, "available": fresh + len(stale_ids), "errors": errors},
    }
    write_json_atomic(DOCS_DIR / "manifiesto.json", manifest, minified=True)
    write_json_atomic(DOCS_DIR / "estado.json", {"schema_version": 1, "generated_at": generated_at, **run}, minified=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Actualiza las 475 referencias de pronóstico del SMN.")
    parser.add_argument("--sleep-seconds", type=float, default=2.0)
    parser.add_argument("--http-attempts", type=int, default=4)
    parser.add_argument("--retry-base-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if args.sleep_seconds < 1.0:
        raise ValueError("--sleep-seconds no puede ser menor que 1.0.")

    config = load_json(CONFIG_FILE)
    references = config.get("forecast_references")
    if not isinstance(references, list) or len(references) != 475:
        raise RuntimeError("La configuración no contiene las 475 referencias esperadas.")
    cache = load_json(CACHE_FILE) if CACHE_FILE.exists() else {"schema_version": 1, "records": {}}
    records = cache.setdefault("records", {})
    session = requests.Session()
    token = get_token(session)
    successes = failures = stale = 0

    for index, target in enumerate(references, start=1):
        reference_id = str(target["forecast_reference_id"])
        previous = records.get(reference_id) if isinstance(records.get(reference_id), dict) else None
        print(f"[{index}/{len(references)}] pronóstico {reference_id}")
        try:
            payload, token, http_attempts = request_with_retries(
                session,
                token,
                fetch_forecast,
                target,
                max_http_attempts=args.http_attempts,
                retry_base_seconds=args.retry_base_seconds,
            )
            historical = bool(payload.get("historical"))
            records[reference_id] = {
                **target,
                "status": "stale" if historical else "success",
                "historical": historical,
                "data_source": payload.get("source", "smn_modern_forecast"),
                "fetched_at": utc_now(),
                "http_attempts_last_run": http_attempts,
                "payload": payload,
            }
            if historical:
                stale += 1
            else:
                successes += 1
            print("  OK HISTÓRICO" if historical else "  OK")
        except Exception as error:
            failures += 1
            if previous and previous.get("payload") is not None:
                records[reference_id] = {
                    **target,
                    "status": "stale",
                    "historical": bool(previous.get("historical")),
                    "data_source": previous.get("data_source"),
                    "fetched_at": previous.get("fetched_at"),
                    "last_refresh_attempt_at": utc_now(),
                    "last_refresh_error": error_record(error),
                    "payload": previous.get("payload"),
                }
                stale += 1
                print(f"  TEMPORAL: se conserva el último dato válido ({error})")
            else:
                records[reference_id] = {
                    **target,
                    "status": "error",
                    "last_refresh_attempt_at": utc_now(),
                    "last_refresh_error": error_record(error),
                }
                print(f"  ERROR: {error}")
        cache["generated_at"] = utc_now()
        write_json_atomic(CACHE_FILE, cache)
        if index < len(references):
            time.sleep(args.sleep_seconds)

    run = {
        "status": "ok" if failures == 0 else "partial",
        "expected": len(references),
        "fresh_queries": successes,
        "stale_queries": stale,
        "failed_queries": failures,
    }
    publish(cache, len(references), run)
    print(f"Pronósticos terminados: {successes} actuales, {stale} históricos/conservados, {failures} fallidos.")


if __name__ == "__main__":
    main()
