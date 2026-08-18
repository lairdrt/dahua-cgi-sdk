"""Bounded single-reader distribution for encoded playback packets."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .models import EncodedMediaPacket

DEFAULT_QUEUE_CAPACITY = 2048
DEFAULT_READ_WINDOW = 0.05


class _PacketSource(Protocol):
    def receive_packets(
        self, duration: float = 1.0
    ) -> tuple[EncodedMediaPacket, ...]: ...


@dataclass(frozen=True, slots=True)
class QueueStatistics:
    """Current bounded-queue pressure counters."""

    capacity: int
    depth: int
    high_water: int
    drops: int


class PacketSubscription:
    """One deterministic, bounded view of a packet fan-out."""

    def __init__(
        self,
        capacity: int,
        *,
        on_close: Callable[[PacketSubscription], None] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._on_close = on_close
        self._items: deque[EncodedMediaPacket] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._drops = 0
        self._high_water = 0

    @property
    def statistics(self) -> QueueStatistics:
        with self._condition:
            return QueueStatistics(
                self._capacity,
                len(self._items),
                self._high_water,
                self._drops,
            )

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def get(self, timeout: float | None = None) -> EncodedMediaPacket | None:
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout)
            return self._items.popleft() if self._items else None

    def close(self) -> None:
        callback = None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._items.clear()
            self._condition.notify_all()
            callback = self._on_close
            self._on_close = None
        if callback is not None:
            callback(self)

    def _put(self, packet: EncodedMediaPacket) -> None:
        with self._condition:
            if self._closed:
                return
            if len(self._items) == self._capacity:
                self._drop_one()
                self._drops += 1
            self._items.append(packet)
            self._high_water = max(self._high_water, len(self._items))
            self._condition.notify()

    def _drop_one(self) -> None:
        """Prefer current RTP while retaining scarce RTCP when practical."""
        for index, packet in enumerate(self._items):
            if packet.packet_type == "rtp":
                del self._items[index]
                return
        self._items.popleft()


class EncodedPacketFanout:
    """Own the sole playback reader and distribute immutable packets."""

    def __init__(
        self,
        source: _PacketSource,
        *,
        read_window: float = DEFAULT_READ_WINDOW,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
    ) -> None:
        if read_window <= 0:
            raise ValueError("read_window must be positive")
        self._source = source
        self._read_window = read_window
        self._queue_capacity = queue_capacity
        self._subscriptions: set[PacketSubscription] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def subscribe(self, *, capacity: int | None = None) -> PacketSubscription:
        subscription = PacketSubscription(
            self._queue_capacity if capacity is None else capacity,
            on_close=self._remove,
        )
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("packet fan-out is closed")
            self._subscriptions.add(subscription)
        return subscription

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("packet fan-out is already started")
            self._thread = threading.Thread(
                name="dahua-packet-fanout", target=self._read_loop
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self._read_window * 4))
        with self._lock:
            subscriptions = tuple(self._subscriptions)
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription.close()

    def _remove(self, subscription: PacketSubscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)

    def _read_loop(self) -> None:
        try:
            while not self._stop.is_set():
                packets = self._source.receive_packets(self._read_window)
                with self._lock:
                    subscriptions = tuple(self._subscriptions)
                for packet in packets:
                    for subscription in subscriptions:
                        subscription._put(packet)
        except Exception as exc:
            if not self._stop.is_set():
                self._error = exc
                self._stop.set()
        finally:
            with self._lock:
                subscriptions = tuple(self._subscriptions)
            for subscription in subscriptions:
                subscription.close()
