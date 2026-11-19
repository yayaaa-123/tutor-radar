from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

import requests


GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
TRANSIT_URL = "https://restapi.amap.com/v5/direction/transit/integrated"
RETRYABLE_AMAP_CODES = frozenset({"10014", "10015", "10016", "10019", "10020", "10021"})


class MapLookupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MapSettings:
    enabled: bool
    key_file: Path
    cache_db: Path
    origin_address: str
    origin_city: str
    strategy: int = 8
    alternative_routes: int = 3
    departure_time: str = "09:00"
    request_timeout_seconds: float = 6.0
    geocode_ttl_days: int = 30
    route_ttl_hours: int = 12
    request_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    request_interval_seconds: float = 0.0
    error_log: Path | None = None
    js_key_file: Path | None = None
    js_security_code_file: Path | None = None
    imported_web_key: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    query: str
    location: str
    citycode: str
    adcode: str | None
    level: str | None
    province: str | None
    city: str | None
    district: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FetchJson = Callable[[str, dict[str, str], float], dict[str, Any]]
Sleep = Callable[[float], None]
Monotonic = Callable[[], float]


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def _polyline_points(value: Any) -> list[list[float]]:
    points: list[list[float]] = []
    for token in _string(value).split(";"):
        parts = token.split(",")
        if len(parts) != 2:
            continue
        try:
            longitude, latitude = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if 70 <= longitude <= 140 and 0 <= latitude <= 60:
            points.append([longitude, latitude])
    return points


def _default_fetch_json(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "tutor-monitor/0.1"},
    )
    response.raise_for_status()
    return response.json()


class MapCache:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=20)
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS map_cache (cache_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS map_request_pacing (scope TEXT PRIMARY KEY, next_allowed REAL NOT NULL)"
        )
        self.connection.commit()

    def reserve_request_slot(
        self,
        scope: str,
        interval_seconds: float,
        now: float,
    ) -> float:
        """Atomically reserve one provider request slot across processes."""
        interval = max(0.0, interval_seconds)
        with closing(sqlite3.connect(self.path, timeout=20, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_allowed FROM map_request_pacing WHERE scope = ?",
                (scope,),
            ).fetchone()
            existing = float(row[0]) if row else now
            # ``time.monotonic()`` resets when Windows restarts, while this
            # pacing row persists in SQLite. Treat an implausibly distant
            # future slot as a stale value from the previous boot instead of
            # sleeping for hours or days.
            maximum_queue_seconds = max(60.0, interval * 60.0)
            if existing - now > maximum_queue_seconds:
                existing = now
            slot = max(now, existing)
            if interval <= 0 and slot <= now:
                connection.execute(
                    "DELETE FROM map_request_pacing WHERE scope = ?",
                    (scope,),
                )
            else:
                connection.execute(
                    """INSERT INTO map_request_pacing(scope, next_allowed) VALUES (?, ?)
                       ON CONFLICT(scope) DO UPDATE SET next_allowed=excluded.next_allowed""",
                    (scope, slot + interval),
                )
            connection.commit()
        return max(0.0, slot - now)

    def cool_down_requests(self, scope: str, seconds: float, now: float) -> None:
        delay = max(0.0, seconds)
        if delay <= 0:
            return
        with closing(sqlite3.connect(self.path, timeout=20, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT next_allowed FROM map_request_pacing WHERE scope = ?",
                (scope,),
            ).fetchone()
            next_allowed = max(float(row[0]) if row else 0.0, now + delay)
            connection.execute(
                """INSERT INTO map_request_pacing(scope, next_allowed) VALUES (?, ?)
                   ON CONFLICT(scope) DO UPDATE SET next_allowed=excluded.next_allowed""",
                (scope, next_allowed),
            )
            connection.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_json FROM map_cache WHERE cache_key = ? AND expires_at > ?",
            (key, time.time()),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, kind: str, payload: dict[str, Any], ttl_seconds: float) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO map_cache(cache_key, kind, payload_json, expires_at) VALUES (?, ?, ?, ?)",
            (key, kind, json.dumps(payload, ensure_ascii=False), time.time() + ttl_seconds),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class AmapCommuteEnricher:
    def __init__(
        self,
        settings: MapSettings,
        fetch_json: FetchJson | None = None,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
    ):
        self.settings = settings
        self.fetch_json = fetch_json or _default_fetch_json
        self.sleep = sleep
        self.monotonic = monotonic
        self.cache = MapCache(settings.cache_db)
        self.api_key = settings.imported_web_key or os.environ.get("AMAP_WEB_KEY", "").strip() or self._read_key_file()
        self._pacing_scope = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest() if self.api_key else "missing-key"

    def _read_key_file(self) -> str:
        try:
            return self.settings.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _cache_key(kind: str, *parts: str) -> str:
        value = "\x1f".join((kind, *parts))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _city_prefix_may_help(error: MapLookupError) -> bool:
        value = str(error)
        return value in {"geocode_no_result", "geocode_incomplete_result"} or value.startswith("amap_3")

    def _request(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        attempts = max(1, self.settings.request_attempts)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            delay = self.cache.reserve_request_slot(
                self._pacing_scope,
                self.settings.request_interval_seconds,
                self.monotonic(),
            )
            if delay > 0:
                self.sleep(delay)
            try:
                payload = self.fetch_json(
                    url,
                    {**params, "key": self.api_key, "output": "json"},
                    self.settings.request_timeout_seconds,
                )
            except Exception as exc:
                last_error = exc
                self._log_network_error(url, exc, attempt, attempts)
                if attempt < attempts:
                    self.sleep(self.settings.retry_backoff_seconds * (2 ** (attempt - 1)))
                    continue
                break

            if _string(payload.get("status")) == "1":
                return payload
            infocode = _string(payload.get("infocode")) or "request_failed"
            self._log_provider_error(url, infocode, attempt, attempts)
            if infocode in RETRYABLE_AMAP_CODES and attempt < attempts:
                self.cache.cool_down_requests(
                    self._pacing_scope,
                    self.settings.retry_backoff_seconds * (2 ** (attempt - 1)),
                    self.monotonic(),
                )
                continue
            raise MapLookupError(f"amap_{infocode}")

        if last_error is not None:
            error_name = type(last_error).__name__ if last_error else "UnknownError"
            raise MapLookupError(f"network_{error_name}") from None
        raise MapLookupError("amap_request_failed")

    def _log_network_error(
        self, url: str, error: Exception, attempt: int, attempts: int
    ) -> None:
        if self.settings.error_log is None:
            return
        reason = getattr(error, "reason", None)
        diagnostic = {
            "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "endpoint": "transit" if url == TRANSIT_URL else "geocode",
            "error_type": type(error).__name__,
            "reason_type": type(reason).__name__ if reason is not None else None,
            "errno": getattr(reason or error, "errno", None),
            "winerror": getattr(reason or error, "winerror", None),
            "attempt": attempt,
            "attempts": attempts,
        }
        try:
            self.settings.error_log.parent.mkdir(parents=True, exist_ok=True)
            with self.settings.error_log.open("a", encoding="utf-8") as file:
                file.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _log_provider_error(
        self, url: str, infocode: str, attempt: int, attempts: int
    ) -> None:
        if self.settings.error_log is None:
            return
        diagnostic = {
            "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "endpoint": "transit" if url == TRANSIT_URL else "geocode",
            "error_type": "AmapResponseError",
            "infocode": infocode,
            "attempt": attempt,
            "attempts": attempts,
        }
        try:
            self.settings.error_log.parent.mkdir(parents=True, exist_ok=True)
            with self.settings.error_log.open("a", encoding="utf-8") as file:
                file.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def geocode(self, address: str, city: str) -> GeocodeResult:
        query = address.strip()
        cache_key = self._cache_key("geocode", city, query)
        cached = self.cache.get(cache_key)
        if cached:
            return GeocodeResult(**cached)
        payload = self._request(GEOCODE_URL, {"address": query, "city": city})
        results = _list(payload.get("geocodes"))
        if not results:
            raise MapLookupError("geocode_no_result")
        item = results[0]
        location = _string(item.get("location"))
        citycode = _string(item.get("citycode"))
        if not location or not citycode:
            raise MapLookupError("geocode_incomplete_result")
        result = GeocodeResult(
            query=query,
            location=location,
            citycode=citycode,
            adcode=_string(item.get("adcode")) or None,
            level=_string(item.get("level")) or None,
            province=_string(item.get("province")) or None,
            city=_string(item.get("city")) or None,
            district=_string(item.get("district")) or None,
        )
        self.cache.put(cache_key, "geocode", result.to_dict(), self.settings.geocode_ttl_days * 86400)
        return result

    def _departure(self) -> tuple[str, str]:
        today = date.today()
        days_until_saturday = (5 - today.weekday()) % 7
        target = today + timedelta(days=days_until_saturday)
        return target.isoformat(), self.settings.departure_time.replace(":", "-")

    def transit(self, origin: GeocodeResult, destination: GeocodeResult) -> dict[str, Any]:
        depart_date, depart_time = self._departure()
        cache_key = self._cache_key(
            "transit", origin.location, destination.location, str(self.settings.strategy), depart_date, depart_time
        )
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        payload = self._request(
            TRANSIT_URL,
            {
                "origin": origin.location,
                "destination": destination.location,
                "city1": origin.citycode,
                "city2": destination.citycode,
                "strategy": str(self.settings.strategy),
                "AlternativeRoute": str(self.settings.alternative_routes),
                "date": depart_date,
                "time": depart_time,
                "show_fields": "cost",
            },
        )
        transits = _list((payload.get("route") or {}).get("transits"))
        alternatives: list[dict[str, Any]] = []
        for transit in transits:
            cost = transit.get("cost") if isinstance(transit.get("cost"), dict) else {}
            duration_text = _string(cost.get("duration") or transit.get("duration"))
            if not duration_text.isdigit():
                continue
            walking = 0
            for segment in _list(transit.get("segments")):
                walking_info = segment.get("walking") if isinstance(segment, dict) else None
                if isinstance(walking_info, dict):
                    distance = _string(walking_info.get("distance"))
                    walking += int(float(distance)) if distance else 0
            alternatives.append(
                {
                    "duration_seconds": int(duration_text),
                    "distance_meters": int(float(_string(transit.get("distance")) or 0)),
                    "walking_distance_meters": walking,
                    "transit_fee_yuan": float(_string(cost.get("transit_fee")) or 0),
                }
            )
        if not alternatives:
            raise MapLookupError("transit_no_duration")
        alternatives.sort(key=lambda item: item["duration_seconds"])
        best = alternatives[0]
        result = {
            **best,
            "duration_minutes": round(best["duration_seconds"] / 60),
            "alternatives": alternatives,
            "strategy": self.settings.strategy,
            "depart_at": f"{depart_date}T{self.settings.departure_time}:00+08:00",
        }
        self.cache.put(cache_key, "transit", result, self.settings.route_ttl_hours * 3600)
        return result

    def transit_detail(self, origin: GeocodeResult, destination: GeocodeResult) -> dict[str, Any]:
        depart_date, depart_time = self._departure()
        cache_key = self._cache_key(
            "transit_detail",
            origin.location,
            destination.location,
            str(self.settings.strategy),
            depart_date,
            depart_time,
        )
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        payload = self._request(
            TRANSIT_URL,
            {
                "origin": origin.location,
                "destination": destination.location,
                "city1": origin.citycode,
                "city2": destination.citycode,
                "strategy": str(self.settings.strategy),
                "AlternativeRoute": "1",
                "date": depart_date,
                "time": depart_time,
                "show_fields": "cost,polyline",
            },
        )
        transits = _list((payload.get("route") or {}).get("transits"))
        if not transits:
            raise MapLookupError("transit_no_route_detail")
        transit = transits[0]
        cost = transit.get("cost") if isinstance(transit.get("cost"), dict) else {}
        duration_text = _string(cost.get("duration") or transit.get("duration"))
        segments: list[dict[str, Any]] = []
        for segment in _list(transit.get("segments")):
            if not isinstance(segment, dict):
                continue
            walking = segment.get("walking") if isinstance(segment.get("walking"), dict) else {}
            for step in _list(walking.get("steps")):
                if not isinstance(step, dict):
                    continue
                path = _polyline_points(step.get("polyline"))
                if len(path) >= 2:
                    segments.append({"mode": "walking", "name": "步行", "path": path})
            bus = segment.get("bus") if isinstance(segment.get("bus"), dict) else {}
            for busline in _list(bus.get("buslines")):
                if not isinstance(busline, dict):
                    continue
                path = _polyline_points(busline.get("polyline"))
                if len(path) >= 2:
                    segments.append(
                        {
                            "mode": "transit",
                            "name": _string(busline.get("name")) or "公共交通",
                            "path": path,
                        }
                    )
        if not segments:
            raise MapLookupError("transit_detail_no_polyline")
        result = {
            "duration_minutes": round(int(duration_text) / 60) if duration_text.isdigit() else None,
            "segments": segments,
        }
        self.cache.put(
            cache_key,
            "transit_detail",
            result,
            self.settings.route_ttl_hours * 3600,
        )
        return result

    def destination_geocode(self, address: str, origin: GeocodeResult) -> GeocodeResult:
        queries = [address.strip()]
        city_prefix = self.settings.origin_city.strip()
        if city_prefix and not queries[0].startswith(city_prefix):
            queries.append(f"{city_prefix}{queries[0]}")
        last_error: MapLookupError | None = None
        for query in queries:
            try:
                result = self.geocode(query, self.settings.origin_city)
            except MapLookupError as exc:
                if not self._city_prefix_may_help(exc):
                    raise
                last_error = exc
                continue
            if result.citycode == origin.citycode:
                return result
            last_error = MapLookupError("geocode_outside_origin_city")
        raise last_error or MapLookupError("geocode_no_result")

    def origin_geocode(self, address: str) -> GeocodeResult:
        queries = [address.strip()]
        city_prefix = self.settings.origin_city.strip()
        if city_prefix and not queries[0].startswith(city_prefix):
            queries.append(f"{city_prefix}{queries[0]}")
        last_error: MapLookupError | None = None
        for query in queries:
            try:
                return self.geocode(query, self.settings.origin_city)
            except MapLookupError as exc:
                if not self._city_prefix_may_help(exc):
                    raise
                last_error = exc
        raise last_error or MapLookupError("geocode_no_result")

    def enrich(self, address: str | None) -> dict[str, Any]:
        return self.enrich_between(self.settings.origin_address, address)

    def enrich_between(self, origin_address: str | None, address: str | None) -> dict[str, Any]:
        """Query public-transit time between an arbitrary origin and an order.

        The legacy monitor still calls ``enrich`` and therefore keeps its
        configured origin. On-demand searches can use a per-user origin and a
        separate area reference point without mutating global settings.
        """
        base = {
            "provider": "amap",
            "mode": "public_transit",
            "origin_address": origin_address,
            "destination_address": address,
        }
        if not self.settings.enabled:
            return {**base, "status": "disabled"}
        if not self.api_key:
            self.api_key = os.environ.get("AMAP_WEB_KEY", "").strip() or self._read_key_file()
        if not self.api_key:
            return {**base, "status": "not_configured", "error": "missing_amap_web_key"}
        if not origin_address:
            return {**base, "status": "error", "error": "missing_origin_address"}
        if not address:
            return {**base, "status": "error", "error": "missing_destination_address"}
        try:
            origin = self.origin_geocode(origin_address)
            destination = self.destination_geocode(address, origin)
            route = self.transit(origin, destination)
            return {
                **base,
                "status": "ok",
                "origin": origin.to_dict(),
                "destination": destination.to_dict(),
                "route": route,
                "queried_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            }
        except (MapLookupError, OSError, ValueError, json.JSONDecodeError) as exc:
            return {**base, "status": "error", "error": str(exc)}

    def route_detail(self, address: str | None) -> dict[str, Any]:
        base = {
            "provider": "amap",
            "mode": "public_transit",
            "origin_address": self.settings.origin_address,
            "destination_address": address,
        }
        if not self.settings.enabled:
            return {**base, "status": "disabled"}
        if not self.api_key:
            self.api_key = os.environ.get("AMAP_WEB_KEY", "").strip() or self._read_key_file()
        if not self.api_key:
            return {**base, "status": "not_configured", "error": "missing_amap_web_key"}
        if not address:
            return {**base, "status": "error", "error": "missing_destination_address"}
        try:
            origin = self.origin_geocode(self.settings.origin_address)
            destination = self.destination_geocode(address, origin)
            route = self.transit_detail(origin, destination)
            return {
                **base,
                "status": "ok",
                "origin": origin.to_dict(),
                "destination": destination.to_dict(),
                "route": route,
                "queried_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            }
        except (MapLookupError, OSError, ValueError, json.JSONDecodeError) as exc:
            return {**base, "status": "error", "error": str(exc)}

    def close(self) -> None:
        self.cache.close()
