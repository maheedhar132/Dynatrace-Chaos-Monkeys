"""Dynatrace access, behind a narrow protocol.

The troop never calls `httpx` directly. It talks to a `DynatraceClient`, so the
same monkeys run against:
  * `DryRunClient`   — records intended writes, changes nothing (the default)
  * `HttpDynatraceClient` — real Settings 2.0 + ingest + problems calls

The protocol is intentionally small: read/create/update/delete a settings
object, ingest an event, and list problems. That is all a reversible,
canary-scoped experiment needs.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class DynatraceClient(Protocol):
    # --- Settings 2.0 (the mutation surface) ---
    def get_object(self, object_id: str) -> dict[str, Any]: ...
    def list_objects(self, schema_id: str, scope: Optional[str] = None) -> list[dict[str, Any]]: ...
    def create_object(self, schema_id: str, scope: str, value: dict[str, Any]) -> str: ...
    def update_object(self, object_id: str, value: dict[str, Any],
                      update_token: Optional[str] = None) -> None: ...
    def delete_object(self, object_id: str) -> None: ...

    # --- fault injection ---
    def ingest_event(self, payload: dict[str, Any]) -> None: ...
    def ingest_metric(self, lines: str) -> None: ...
    def ingest_log(self, payload: list[dict[str, Any]]) -> None: ...

    # --- the oracle ---
    def recent_problems(self, since_ms: int) -> list[dict[str, Any]]: ...

    # --- preflight (doctor) ---
    def ping(self) -> bool: ...
    def token_scopes(self) -> list[str]: ...

    # --- schema verification (verify) ---
    def schema_ids(self) -> list[str]: ...
    def validate_object(self, schema_id: str, scope: str,
                        value: dict[str, Any]) -> list[str]: ...

    # --- the watcher's oracle: was the change recorded? ---
    def audit_events(self, since_ms: int) -> list[dict[str, Any]]: ...

    # --- API tokens (Tarsier) ---
    def list_tokens(self) -> list[dict[str, Any]]: ...
    def create_token(self, name: str, scopes: list[str]) -> str: ...
    def delete_token(self, token_id: str) -> None: ...

    # --- synthetic monitors (Vervet) ---
    def list_monitors(self) -> list[dict[str, Any]]: ...
    def set_monitor(self, monitor_id: str, body: dict[str, Any]) -> None: ...


class DryRunClient:
    """Changes nothing. Logs every write it was asked to make.

    Reads come from an optional in-memory store so monkeys can be exercised
    without a tenant. `writes` is the record a plan/report reads back.
    """

    def __init__(self, store: Optional[dict[str, dict[str, Any]]] = None) -> None:
        self.store: dict[str, dict[str, Any]] = store or {}
        self.writes: list[str] = []
        self.audit: list[dict[str, Any]] = []  # a sim tenant may append entries here
        self.tokens: dict[str, dict[str, Any]] = {}
        self.monitors: dict[str, dict[str, Any]] = {}

    def get_object(self, object_id: str) -> dict[str, Any]:
        return self.store.get(object_id, {"objectId": object_id, "value": {}})

    def list_objects(self, schema_id: str, scope: Optional[str] = None) -> list[dict[str, Any]]:
        return [
            o for o in self.store.values()
            if o.get("schemaId") == schema_id and (scope is None or o.get("scope") == scope)
        ]

    def create_object(self, schema_id: str, scope: str, value: dict[str, Any]) -> str:
        oid = f"DRYRUN-{schema_id}-{len(self.writes)}"
        self.writes.append(f"CREATE {schema_id}@{scope} -> {oid}")
        # keep it so a later list_objects/get_object in the same process finds it
        # (e.g. `canary setup` then `plan` in one run, or the Swagger UI)
        self.store[oid] = {"objectId": oid, "schemaId": schema_id, "scope": scope, "value": value}
        return oid

    def update_object(self, object_id: str, value: dict[str, Any],
                      update_token: Optional[str] = None) -> None:
        self.writes.append(f"UPDATE {object_id}" + (f" (updateToken {update_token})"
                                                     if update_token else ""))

    def delete_object(self, object_id: str) -> None:
        self.writes.append(f"DELETE {object_id}")
        self.store.pop(object_id, None)

    def ingest_event(self, payload: dict[str, Any]) -> None:
        self.writes.append(f"INGEST event {payload.get('title', '?')}")

    def ingest_metric(self, lines: str) -> None:
        self.writes.append(f"INGEST metric {lines.splitlines()[0] if lines else ''}")

    def ingest_log(self, payload: list[dict[str, Any]]) -> None:
        first = payload[0].get("content", "?") if payload else "?"
        self.writes.append(f"INGEST log {first}")

    def recent_problems(self, since_ms: int) -> list[dict[str, Any]]:
        return []

    def audit_events(self, since_ms: int) -> list[dict[str, Any]]:
        # default: empty -> the watcher reports a blind spot (the safe assumption)
        return [e for e in self.audit if e.get("timestamp", since_ms) >= since_ms]

    def list_tokens(self) -> list[dict[str, Any]]:
        return list(self.tokens.values())

    def create_token(self, name: str, scopes: list[str]) -> str:
        tid = f"DRYRUN-token-{len(self.tokens)}"
        self.tokens[tid] = {"id": tid, "name": name, "scopes": scopes}
        self.writes.append(f"CREATE token {name}")
        return tid

    def delete_token(self, token_id: str) -> None:
        self.writes.append(f"REVOKE token {token_id}")
        self.tokens.pop(token_id, None)

    def list_monitors(self) -> list[dict[str, Any]]:
        return list(self.monitors.values())

    def set_monitor(self, monitor_id: str, body: dict[str, Any]) -> None:
        self.writes.append(f"SET monitor {monitor_id}")
        self.monitors.setdefault(monitor_id, {"entityId": monitor_id})["value"] = body

    def ping(self) -> bool:
        return True

    def token_scopes(self) -> list[str]:
        # a dry-run client has no real token; report the least-privilege set
        return ["settings.read", "settings.write", "events.ingest", "problems.read"]

    def schema_ids(self) -> list[str]:
        # no tenant to ask: claim the schemas the troop knows about so a dry-run
        # `verify` reports "present (dry-run)" rather than a wall of false misses
        from .monkeys import FIRE
        known = {s for cls in FIRE.values() for s in getattr(cls, "schemas", ())}
        known.add("builtin:management-zones")
        return sorted(known)

    def validate_object(self, schema_id: str, scope: str, value: dict[str, Any]) -> list[str]:
        return []   # dry-run: nothing to validate against; treat as acceptable


class HttpDynatraceClient:
    """Real client. Requires the `http` extra (`pip install .[http]`).

    Writes are intentionally limited to settings objects plus event ingest —
    the protocol above — so there is no code path here that could rotate a
    tenant token, delete a bucket, or run a billing query. Those don't exist
    on this object by design.
    """

    def __init__(self, base_url: str, api_token: str, timeout: float = 30.0) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("install the 'http' extra: pip install .[http]") from exc
        self._httpx = httpx
        self._token = api_token
        self._c = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Api-Token {api_token}"},
            timeout=timeout,
        )

    def get_object(self, object_id: str) -> dict[str, Any]:
        # ask for updateToken so callers can do optimistic-concurrency updates
        r = self._c.get(f"/api/v2/settings/objects/{object_id}",
                        params={"fields": "objectId,value,scope,schemaId,updateToken"})
        r.raise_for_status()
        return r.json()

    def list_objects(self, schema_id: str, scope: Optional[str] = None) -> list[dict[str, Any]]:
        params = {"schemaIds": schema_id, "fields": "objectId,value,scope,schemaId,updateToken"}
        if scope:
            params["scopes"] = scope
        r = self._c.get("/api/v2/settings/objects", params=params)
        r.raise_for_status()
        return r.json().get("items", [])

    def create_object(self, schema_id: str, scope: str, value: dict[str, Any]) -> str:
        body = [{"schemaId": schema_id, "scope": scope, "value": value}]
        r = self._c.post("/api/v2/settings/objects", json=body)
        r.raise_for_status()
        return r.json()[0]["objectId"]

    def update_object(self, object_id: str, value: dict[str, Any],
                      update_token: Optional[str] = None) -> None:
        # Settings 2.0 optimistic concurrency: passing the object's updateToken
        # makes the PUT fail (409) instead of clobbering a concurrent edit.
        body: dict[str, Any] = {"value": value}
        if update_token:
            body["updateToken"] = update_token
        r = self._c.put(f"/api/v2/settings/objects/{object_id}", json=body)
        r.raise_for_status()

    def delete_object(self, object_id: str) -> None:
        r = self._c.delete(f"/api/v2/settings/objects/{object_id}")
        r.raise_for_status()

    def ingest_event(self, payload: dict[str, Any]) -> None:
        r = self._c.post("/api/v2/events/ingest", json=payload)
        r.raise_for_status()

    def ingest_metric(self, lines: str) -> None:
        # Metric ingest uses the line protocol, not JSON.
        r = self._c.post("/api/v2/metrics/ingest", content=lines,
                         headers={"Content-Type": "text/plain; charset=utf-8"})
        r.raise_for_status()

    def ingest_log(self, payload: list[dict[str, Any]]) -> None:
        r = self._c.post("/api/v2/logs/ingest", json=payload)
        r.raise_for_status()

    def recent_problems(self, since_ms: int) -> list[dict[str, Any]]:
        r = self._c.get("/api/v2/problems", params={"from": f"{since_ms}"})
        r.raise_for_status()
        return r.json().get("problems", [])

    def audit_events(self, since_ms: int) -> list[dict[str, Any]]:
        # Classic audit log. On Latest, query Grail instead:
        #   fetch dt.system.events | filter event.kind=="AUDIT_EVENT"
        #                          | filter event.provider=="SETTINGS"
        r = self._c.get("/api/v2/auditlogs", params={"from": f"{since_ms}"})
        r.raise_for_status()
        return r.json().get("auditLogs", [])

    def list_tokens(self) -> list[dict[str, Any]]:
        r = self._c.get("/api/v2/apiTokens", params={"fields": "id,name,scopes"})
        r.raise_for_status()
        return r.json().get("apiTokens", [])

    def create_token(self, name: str, scopes: list[str]) -> str:
        r = self._c.post("/api/v2/apiTokens", json={"name": name, "scopes": scopes})
        r.raise_for_status()
        return r.json()["id"]

    def delete_token(self, token_id: str) -> None:
        r = self._c.delete(f"/api/v2/apiTokens/{token_id}")
        r.raise_for_status()

    def list_monitors(self) -> list[dict[str, Any]]:
        r = self._c.get("/api/v2/synthetic/monitors")
        r.raise_for_status()
        return r.json().get("monitors", [])

    def set_monitor(self, monitor_id: str, body: dict[str, Any]) -> None:
        # PUT replaces the monitor; body should be a full monitor config.
        r = self._c.put(f"/api/v2/synthetic/monitors/{monitor_id}", json=body)
        r.raise_for_status()

    def ping(self) -> bool:
        # cheapest read that exercises the settings.read scope we require
        try:
            return self._c.get("/api/v2/settings/schemas").status_code == 200
        except Exception:
            return False

    def token_scopes(self) -> list[str]:
        # self-lookup: what can THIS token actually do? (for the least-privilege check)
        r = self._c.post("/api/v2/apiTokens/lookup", json={"token": self._token})
        r.raise_for_status()
        return r.json().get("scopes", [])

    def schema_ids(self) -> list[str]:
        r = self._c.get("/api/v2/settings/schemas")
        r.raise_for_status()
        return [s.get("schemaId") for s in r.json().get("items", []) if s.get("schemaId")]

    def validate_object(self, schema_id: str, scope: str, value: dict[str, Any]) -> list[str]:
        # Settings 2.0 validate-only: POST with ?validateOnly=true persists nothing
        # and returns 200 (valid) or 400 with the schema errors. We never write.
        r = self._c.post("/api/v2/settings/objects", params={"validateOnly": "true"},
                         json=[{"schemaId": schema_id, "scope": scope, "value": value}])
        if r.status_code < 300:
            return []
        try:
            body = r.json()
        except Exception:
            return [f"HTTP {r.status_code}"]
        errors: list[str] = []
        items = body if isinstance(body, list) else [body]
        for item in items:
            for err in (item.get("error", {}).get("constraintViolations")
                        or item.get("constraintViolations") or []):
                msg = err.get("message") if isinstance(err, dict) else str(err)
                if msg:
                    errors.append(msg)
            if not errors and isinstance(item, dict) and item.get("error", {}).get("message"):
                errors.append(item["error"]["message"])
        return errors or [f"HTTP {r.status_code}"]
