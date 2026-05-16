"""EventBus — in-process pub/sub for memory lifecycle events.

Adapters subscribe to events using :meth:`EventBus.subscribe`.  The
gateway emits events using :meth:`EventBus.emit`.  All dispatch is
synchronous and in-process; shell-hook execution is handled by the
separate :class:`~core.hooks.HookDispatcher`.

Typical usage::

    bus = EventBus()

    # Adapter registration (once at startup / gateway construction)
    bus.subscribe("post-promote", my_handler)

    # Gateway emission (after every promotion)
    bus.emit("post-promote", {
        "item_id": "abc-123",
        "content_preview": "Architecture decision: …",
        "from_layer": "session",
        "to_layer": "project",
    })

Handler signature::

    def handler(payload: dict) -> None: ...

Errors raised inside a handler are caught and logged so that one
broken subscriber never silently kills the fan-out to the rest.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

logger = logging.getLogger(__name__)

# Canonical event names used by MemoryGateway.
POST_CAPTURE = "post-capture"
POST_PROMOTE = "post-promote"
POST_ARCHIVE = "post-archive"
POST_FORGET = "post-forget"

# All valid event names (superset of HookDispatcher.VALID_EVENTS).
VALID_EVENTS = [POST_CAPTURE, POST_PROMOTE, POST_ARCHIVE, POST_FORGET]

Handler = Callable[[dict], None]


class EventBus:
    """Lightweight in-process publish/subscribe event bus.

    All events are dispatched **synchronously** in the order handlers
    were subscribed.  Handlers must be non-blocking; long-running work
    should be deferred to a thread/process by the handler itself.

    This class is intentionally dependency-free so it can be imported
    without pulling in any mnemos sub-module.
    """

    def __init__(self) -> None:
        # {event_name: [handler, ...]}  — list preserves subscription order
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    # ------------------------------------------------------------------ #
    # Subscription                                                          #
    # ------------------------------------------------------------------ #

    def subscribe(self, event_name: str, handler: Handler) -> None:
        """Register *handler* to be called whenever *event_name* is emitted.

        The same handler may be registered multiple times; each
        registration results in one additional call per emission.

        Args:
            event_name: The event to subscribe to.  Any string is
                accepted (no validation) so that callers can define
                their own namespaced events without modifying this
                module.
            handler: A callable that accepts a single ``dict`` argument
                (the event payload) and returns ``None``.
        """
        self._handlers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: Handler) -> None:
        """Remove the first matching registration of *handler* for *event_name*.

        No-op if the handler is not registered for that event.
        """
        try:
            self._handlers[event_name].remove(handler)
        except ValueError:
            pass

    # ------------------------------------------------------------------ #
    # Emission                                                              #
    # ------------------------------------------------------------------ #

    def emit(self, event_name: str, payload: dict) -> None:
        """Fan-out *payload* to all handlers registered for *event_name*.

        Errors raised by individual handlers are caught, logged at
        WARNING level, and do not prevent other handlers from receiving
        the event.  The caller never sees handler exceptions.

        Args:
            event_name: The event to emit.
            payload: Arbitrary dict passed verbatim to every handler.
        """
        for handler in list(self._handlers.get(event_name, [])):
            try:
                handler(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EventBus handler %r raised for event %r: %s",
                    handler,
                    event_name,
                    exc,
                )

    # ------------------------------------------------------------------ #
    # Introspection                                                         #
    # ------------------------------------------------------------------ #

    def handler_count(self, event_name: str) -> int:
        """Return the number of handlers registered for *event_name*."""
        return len(self._handlers.get(event_name, []))

    def clear(self, event_name: str | None = None) -> None:
        """Remove all handlers, optionally scoped to a single *event_name*.

        Primarily useful in test teardown.
        """
        if event_name is None:
            self._handlers.clear()
        else:
            self._handlers.pop(event_name, None)
