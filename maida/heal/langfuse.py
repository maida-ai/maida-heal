"""Minimal read-only Langfuse discovery client with selective field retrieval."""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

DEFAULT_METADATA_KEYS = (
    "agent_id",
    "agent",
    "gateway",
    "gateway_id",
    "workflow",
    "workflow_id",
    "service",
)


class LangfuseError(RuntimeError):
    """A read-only connection or response-shape error."""


@dataclass(frozen=True)
class LangfuseCredentials:
    public_key: str
    secret_key: str
    host: str
    source: str


def _dotenv_values(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without evaluating shell syntax."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_credentials(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> LangfuseCredentials:
    """Resolve SDK-compatible variables, then common local environment files."""
    current = dict(os.environ if environ is None else environ)
    candidates: list[tuple[str, Mapping[str, str]]] = [("environment", current)]
    for path in (
        project_root / ".env",
        project_root / ".env.local",
        Path.home() / ".config" / "langfuse" / ".env",
    ):
        candidates.append((f"config:{path}", _dotenv_values(path)))

    for source, values in candidates:
        public_key = values.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = values.get("LANGFUSE_SECRET_KEY", "").strip()
        host = (
            values.get("LANGFUSE_BASE_URL", "").strip()
            or values.get("LANGFUSE_HOST", "").strip()
            or "https://cloud.langfuse.com"
        )
        if public_key and secret_key:
            return LangfuseCredentials(
                public_key=public_key,
                secret_key=secret_key,
                host=host.rstrip("/"),
                source="environment" if source == "environment" else "config",
            )
    raise LangfuseError(
        "Langfuse credentials were not found. In Langfuse, open Project settings "
        "→ API Keys, then export LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY "
        "(plus LANGFUSE_HOST for a regional or self-hosted instance)."
    )


@dataclass(frozen=True)
class Observation:
    """Payload-free observation projection used only for stream discovery."""

    id: str
    trace_id: str
    trace_name: str
    session_id: str | None
    start_time: datetime
    end_time: datetime | None
    observation_type: str
    name: str
    metadata: dict[str, str]
    usage_total: float
    total_cost: float


class LangfuseClient(Protocol):
    def validate(self) -> None: ...

    def discover(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        metadata_keys: Iterable[str],
    ) -> list[Observation]: ...


def _parse_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise LangfuseError(f"Langfuse observation has no valid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LangfuseError(f"Langfuse observation has invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LangfuseError(f"Langfuse observation {field} must include a timezone")
    return parsed


def _safe_scalar(value: object) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and 0 < len(value) <= 200:
        return value
    return None


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


class HTTPClient:
    """Read only selective fields from the Langfuse observations v2 endpoint."""

    def __init__(
        self, credentials: LangfuseCredentials, *, timeout: float = 10.0
    ) -> None:
        parsed = urlsplit(credentials.host)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise LangfuseError(
                "LANGFUSE_HOST must be an http(s) origin without credentials, "
                "a path, query, or fragment"
            )
        if timeout <= 0:
            raise ValueError("Langfuse timeout must be positive")
        self.credentials = credentials
        self.timeout = timeout

    def _get(self, params: Mapping[str, object]) -> dict[str, Any]:
        query = urlencode(params)
        url = f"{self.credentials.host}/api/public/v2/observations?{query}"
        token = base64.b64encode(
            f"{self.credentials.public_key}:{self.credentials.secret_key}".encode()
        ).decode("ascii")
        request = Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "Authorization": f"Basic {token}"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise LangfuseError(
                f"Langfuse read-only request returned HTTP {error.code}. "
                "Check the project API keys and host."
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            reason = (
                str(error.reason)
                if isinstance(error, URLError)
                else type(error).__name__
            )
            raise LangfuseError(
                f"Could not reach the configured Langfuse host: {reason}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LangfuseError("Langfuse returned an invalid JSON response") from error
        if not isinstance(payload, dict):
            raise LangfuseError("Langfuse returned a non-object response")
        return payload

    def validate(self) -> None:
        """Perform one minimal metadata-only request; never request I/O fields."""
        payload = self._get({"fields": "core", "limit": 1})
        if not isinstance(payload.get("data"), list):
            raise LangfuseError("Langfuse validation response has no data array")

    def discover(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        metadata_keys: Iterable[str],
    ) -> list[Observation]:
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (from_time, to_time)
        ):
            raise ValueError("discovery bounds must be timezone-aware")
        if to_time <= from_time:
            raise ValueError("discovery end must be after its start")
        selected_keys = tuple(dict.fromkeys(metadata_keys))
        cursor: str | None = None
        rows: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, object] = {
                "fields": "core,basic,metadata,usage,metrics,trace_context",
                "fromStartTime": from_time.isoformat(),
                "toStartTime": to_time.isoformat(),
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._get(params)
            page = payload.get("data")
            if not isinstance(page, list) or not all(
                isinstance(item, dict) for item in page
            ):
                raise LangfuseError("Langfuse discovery response has invalid data")
            rows.extend(page)
            meta = payload.get("meta")
            next_cursor = meta.get("cursor") if isinstance(meta, dict) else None
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise LangfuseError("Langfuse discovery returned an invalid cursor")
            if next_cursor in seen_cursors:
                raise LangfuseError("Langfuse discovery repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        observations: list[Observation] = []
        seen_ids: set[str] = set()
        for row in rows:
            identifier = row.get("id")
            trace_id = row.get("traceId")
            if not isinstance(identifier, str) or not isinstance(trace_id, str):
                raise LangfuseError("Langfuse observation has no id or traceId")
            if identifier in seen_ids:
                continue
            seen_ids.add(identifier)
            raw_metadata = row.get("metadata")
            metadata: dict[str, str] = {}
            if isinstance(raw_metadata, dict):
                for key in selected_keys:
                    scalar = _safe_scalar(raw_metadata.get(key))
                    if scalar is not None:
                        metadata[key] = scalar
            usage = row.get("usageDetails")
            usage_total = _number(usage.get("total")) if isinstance(usage, dict) else 0
            cost = row.get("costDetails")
            total_cost = _number(cost.get("total")) if isinstance(cost, dict) else 0
            if not total_cost:
                total_cost = _number(row.get("totalCost"))
            session = row.get("sessionId")
            end = row.get("endTime")
            trace_name_value = row.get("traceName")
            observation_name = row.get("name")
            trace_name = (
                trace_name_value
                if isinstance(trace_name_value, str) and trace_name_value
                else observation_name
                if isinstance(observation_name, str) and observation_name
                else "unnamed-agent"
            )
            observations.append(
                Observation(
                    id=identifier,
                    trace_id=trace_id,
                    trace_name=trace_name,
                    session_id=session
                    if isinstance(session, str) and session
                    else None,
                    start_time=_parse_datetime(row.get("startTime"), "startTime"),
                    end_time=_parse_datetime(end, "endTime")
                    if end is not None
                    else None,
                    observation_type=str(row.get("type") or "SPAN"),
                    name=str(row.get("name") or "unnamed"),
                    metadata=metadata,
                    usage_total=usage_total,
                    total_cost=total_cost,
                )
            )
        return observations


class FixtureClient:
    """Offline client used by executable walkthroughs and tests."""

    def __init__(self, observations: Iterable[Observation]) -> None:
        self.observations = list(observations)
        self.validation_calls = 0
        self.discovery_windows: list[tuple[datetime, datetime]] = []

    def validate(self) -> None:
        self.validation_calls += 1

    def discover(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        metadata_keys: Iterable[str],
    ) -> list[Observation]:
        del metadata_keys
        self.discovery_windows.append((from_time, to_time))
        return [
            item for item in self.observations if from_time <= item.start_time < to_time
        ]
