"""
Minimal HTTP client for the Graphiti MCP server.

Speaks JSON-RPC 2.0 over the streamable-HTTP transport (single endpoint,
session id returned on initialize, kept in the Mcp-Session-Id header for
subsequent requests). Uses stdlib only — no pip install required.

Use:
    client = GraphitiClient("http://localhost:8765/mcp")
    client.initialize()
    client.add_memory(
        group_id="fleet",
        name="example",
        episode_body="Episode text body.",
        source="text",
        source_description="ingest test",
    )
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class GraphitiError(Exception):
    code: int | None
    message: str
    data: Any = None

    def __str__(self) -> str:
        return f"Graphiti error {self.code}: {self.message}"


class GraphitiClient:
    """Thin JSON-RPC client over Graphiti's HTTP MCP transport."""

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        # The server 307-redirects /mcp/ -> /mcp. We strip the trailing slash
        # ourselves because urllib does not re-POST after a 307.
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session_id: str | None = None
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(
        self, body: dict[str, Any], expect_response: bool = True
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """POST a JSON-RPC frame. When expect_response is False (notifications),
        the server returns 202 with an empty body — we accept that and return None.
        """
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise GraphitiError(code=e.code, message=f"HTTP {e.code}: {body_text}")
        except urllib.error.URLError as e:
            raise GraphitiError(code=None, message=f"Connection failed: {e.reason}")

        if not expect_response:
            return None, resp_headers

        # The server emits Server-Sent-Events framing even for single responses
        # ("event: message\ndata: {...}\n\n"). Strip framing if present.
        payload_line = raw
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload_line = line[len("data:") :].strip()
                break

        try:
            payload = json.loads(payload_line)
        except json.JSONDecodeError as e:
            raise GraphitiError(code=None, message=f"Bad JSON from server: {e} / raw={raw[:200]!r}")

        if "error" in payload:
            err = payload["error"]
            raise GraphitiError(
                code=err.get("code"),
                message=err.get("message", "unknown"),
                data=err.get("data"),
            )

        return payload, resp_headers

    def initialize(self) -> dict[str, Any]:
        """Open a session. Captures Mcp-Session-Id for subsequent calls."""
        payload, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pb-graphiti-ingest", "version": "0.1"},
                },
            }
        )
        self.session_id = headers.get("mcp-session-id")
        # Required by spec — send notification that we're ready. Notifications
        # in JSON-RPC have no `id` and get no response (202 + empty body).
        self._post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            expect_response=False,
        )
        return payload.get("result", {}) if payload else {}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        payload, _ = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert payload is not None
        result = payload.get("result", {})
        content = result.get("content")
        if isinstance(content, list) and content and content[0].get("type") == "text":
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result

    def add_memory(
        self,
        *,
        group_id: str,
        name: str,
        episode_body: str,
        source: str = "text",
        source_description: str = "",
        reference_time: str | None = None,
        previous_episode_uuids: Iterable[str] | None = None,
    ) -> Any:
        args: dict[str, Any] = {
            "group_id": group_id,
            "name": name,
            "episode_body": episode_body,
            "source": source,
            "source_description": source_description,
        }
        if reference_time:
            args["reference_time"] = reference_time
        if previous_episode_uuids:
            args["previous_episode_uuids"] = list(previous_episode_uuids)
        return self.call_tool("add_memory", args)
