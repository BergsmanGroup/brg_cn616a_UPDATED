"""Bounded asynchronous JSON Lines client for the local controller service."""

from __future__ import annotations

import copy
import json
import logging
import queue
import socket
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional, Tuple


LOGGER = logging.getLogger("cn616a.gui")
_MAX_RESPONSE_BYTES = 64 * 1024

SubmitStatus = Literal["submitted", "coalesced", "full", "closed"]
ResultCallback = Callable[[Dict[str, Any]], None]


@dataclass(frozen=True)
class _Request:
    token: int
    host: str
    port: int
    messages: Tuple[Dict[str, Any], ...]
    priority: int
    coalesce_key: Optional[str]


class ServiceClient:
    """Serialize service requests and deliver results when polled by the caller."""

    def __init__(self, *, capacity: int = 32, timeout_s: float = 2.0):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._capacity = capacity
        self._timeout_s = float(timeout_s)
        self._requests: queue.PriorityQueue[tuple[int, int, _Request]] = queue.PriorityQueue(maxsize=capacity)
        self._results: queue.Queue[tuple[int, Dict[str, Any]]] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._callbacks: Dict[int, ResultCallback] = {}
        self._coalesced: set[str] = set()
        self._outstanding = 0
        self._sequence = 0
        self._next_token = 0
        self._stopping = threading.Event()
        self._worker = threading.Thread(target=self._run, name="cn616a-service-client", daemon=True)
        self._worker.start()

    def submit(
        self,
        host: str,
        port: int,
        messages: list[Dict[str, Any]],
        callback: ResultCallback,
        *,
        priority: int = 0,
        coalesce_key: Optional[str] = None,
    ) -> SubmitStatus:
        if not messages:
            raise ValueError("at least one service message is required")
        copied_messages = tuple(copy.deepcopy(message) for message in messages)
        with self._lock:
            if self._stopping.is_set():
                return "closed"
            if coalesce_key is not None and coalesce_key in self._coalesced:
                return "coalesced"
            if self._outstanding >= self._capacity:
                return "full"

            self._next_token += 1
            token = self._next_token
            self._sequence += 1
            for message in copied_messages:
                message.setdefault("id", uuid.uuid4().hex)

            request = _Request(
                token=token,
                host=str(host),
                port=int(port),
                messages=copied_messages,
                priority=int(priority),
                coalesce_key=coalesce_key,
            )
            try:
                self._requests.put_nowait((request.priority, self._sequence, request))
            except queue.Full:
                return "full"
            self._callbacks[token] = callback
            self._outstanding += 1
            if coalesce_key is not None:
                self._coalesced.add(coalesce_key)
            return "submitted"

    def poll_results(self) -> int:
        """Deliver completed work; call from the GUI event thread."""
        delivered = 0
        while True:
            try:
                token, result = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                callback = self._callbacks.pop(token, None)
                self._outstanding = max(0, self._outstanding - 1)
                key = result.get("coalesce_key")
                if key is not None:
                    self._coalesced.discard(str(key))
            if callback is not None:
                try:
                    callback(result)
                except Exception:
                    LOGGER.exception("GUI failed applying service response %s", token)
            delivered += 1
        return delivered

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work; optionally wait for the bounded in-flight socket operation."""
        with self._lock:
            self._stopping.set()
        if wait:
            self._worker.join(timeout=(2.0 * self._timeout_s) + 1.0)
            if self._worker.is_alive():
                LOGGER.error("Service client worker did not stop within its socket timeout")
        elif self._worker.is_alive():
            LOGGER.info("Service client worker will exit after its bounded in-flight socket operation")

        while True:
            try:
                _, _, request = self._requests.get_nowait()
            except queue.Empty:
                break
            operations = ", ".join(str(message.get("op", "unknown")) for message in request.messages)
            LOGGER.warning(
                "Service request %s was not sent because the GUI shut down: %s",
                request.token,
                operations,
            )
            result = {
                "ok": False,
                "outcomes": [
                    self._outcome(message, "not_sent", "GUI shut down before the request was sent")
                    for message in request.messages
                ],
                "coalesce_key": request.coalesce_key,
            }
            try:
                self._results.put_nowait((request.token, result))
            except queue.Full:
                LOGGER.error(
                    "Unable to retain shutdown outcome for unsent request %s",
                    request.token,
                )

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                _, _, request = self._requests.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                result = self._execute(request)
            except Exception as exc:
                LOGGER.exception("Unexpected service-client worker failure for request %s", request.token)
                result = {
                    "ok": False,
                    "outcomes": [
                        self._outcome(
                            message,
                            "unknown",
                            f"Service worker failed: {type(exc).__name__}: {exc}",
                        )
                        for message in request.messages
                    ],
                    "coalesce_key": request.coalesce_key,
                }
            if self._stopping.is_set():
                LOGGER.warning("Service request %s completed during GUI shutdown: %s", request.token, result["outcomes"])
            self._results.put((request.token, result))

    def _execute(self, request: _Request) -> Dict[str, Any]:
        outcomes = []
        for index, message in enumerate(request.messages):
            if self._stopping.is_set():
                outcomes.extend(
                    self._outcome(queued, "not_sent", "GUI is shutting down")
                    for queued in request.messages[index:]
                )
                break
            outcome = self._send_one(request.host, request.port, message)
            outcomes.append(outcome)
            if outcome["state"] != "confirmed_success":
                outcomes.extend(
                    self._outcome(queued, "not_sent", "Earlier operation did not succeed")
                    for queued in request.messages[index + 1 :]
                )
                break
        return {
            "ok": bool(outcomes) and all(item["state"] == "confirmed_success" for item in outcomes),
            "outcomes": outcomes,
            "coalesce_key": request.coalesce_key,
        }

    def _send_one(self, host: str, port: int, message: Dict[str, Any]) -> Dict[str, Any]:
        request_id = message.get("id")
        try:
            encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            return self._outcome(message, "not_sent", f"Request could not be encoded: {exc}")

        sent = False
        try:
            with socket.create_connection((host, port), timeout=self._timeout_s) as connection:
                connection.settimeout(self._timeout_s)
                sent = True
                connection.sendall(encoded)
                response_bytes = bytearray()
                while b"\n" not in response_bytes:
                    chunk = connection.recv(min(4096, _MAX_RESPONSE_BYTES + 1 - len(response_bytes)))
                    if not chunk:
                        raise ConnectionError("service closed before a complete JSON Lines response")
                    response_bytes.extend(chunk)
                    if len(response_bytes) > _MAX_RESPONSE_BYTES:
                        raise ValueError("service response exceeded the size limit")
        except (OSError, ValueError) as exc:
            state = "unknown" if sent else "not_sent"
            return self._outcome(message, state, f"{type(exc).__name__}: {exc}")

        response_line = bytes(response_bytes).split(b"\n", 1)[0].strip()
        try:
            response = json.loads(response_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._outcome(message, "unknown", f"Malformed service response: {exc}")
        if not isinstance(response, dict):
            return self._outcome(message, "unknown", "Service response was not a JSON object")
        if response.get("id") != request_id:
            return self._outcome(message, "unknown", "Service response ID did not match the request", response)

        state = "confirmed_success" if response.get("ok") is True else "confirmed_failure"
        detail = None if state == "confirmed_success" else str(response.get("error", "service rejected the command"))
        return self._outcome(message, state, detail, response)

    @staticmethod
    def _outcome(
        message: Dict[str, Any],
        state: str,
        error: Optional[str],
        response: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        outcome: Dict[str, Any] = {
            "id": message.get("id"),
            "op": message.get("op", "unknown"),
            "state": state,
        }
        if error is not None:
            outcome["error"] = error
        if response is not None:
            outcome["response"] = response
        return outcome
