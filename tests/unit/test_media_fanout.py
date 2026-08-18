import threading
import time
from unittest import TestCase

from dahua_rpc import EncodedMediaPacket
from dahua_rpc._media_fanout import EncodedPacketFanout


class EncodedPacketFanoutTests(TestCase):
    def test_one_reader_distributes_ordered_packets_to_multiple_consumers(self) -> None:
        source = _Source([(_packet(1), _packet(2), _packet(3))])
        fanout = EncodedPacketFanout(source, read_window=0.01, queue_capacity=4)
        first = fanout.subscribe()
        second = fanout.subscribe()

        fanout.start()
        self.assertTrue(source.called.wait(1.0))

        self.assertEqual([first.get(1).sequence_number for _ in range(3)], [1, 2, 3])
        self.assertEqual([second.get(1).sequence_number for _ in range(3)], [1, 2, 3])
        self.assertEqual(source.max_concurrent, 1)
        fanout.close()

    def test_slow_consumer_drops_oldest_rtp_but_keeps_recent_media(self) -> None:
        source = _Source([tuple(_packet(value) for value in range(5))])
        fanout = EncodedPacketFanout(source, read_window=0.01, queue_capacity=2)
        subscription = fanout.subscribe()

        fanout.start()
        self.assertTrue(source.called.wait(1.0))
        _wait_for(lambda: subscription.statistics.drops == 3)

        self.assertEqual(subscription.statistics.high_water, 2)
        self.assertEqual(subscription.statistics.drops, 3)
        self.assertEqual(
            [subscription.get(0).sequence_number for _ in range(2)], [3, 4]
        )
        fanout.close()

    def test_subscription_removal_and_shutdown_are_deterministic(self) -> None:
        source = _Source([])
        fanout = EncodedPacketFanout(source, read_window=0.01)
        subscription = fanout.subscribe()
        subscription.close()

        fanout.start()
        fanout.close()

        self.assertTrue(subscription.closed)
        self.assertFalse(fanout.running)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            fanout.subscribe()


class _Source:
    def __init__(self, batches: list[tuple[EncodedMediaPacket, ...]]) -> None:
        self.batches = batches
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.called = threading.Event()
        self.lock = threading.Lock()

    def receive_packets(self, duration: float) -> tuple[EncodedMediaPacket, ...]:
        with self.lock:
            self.calls += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            batch = self.batches.pop(0) if self.batches else ()
            self.called.set()
            time.sleep(min(duration, 0.005))
            return batch
        finally:
            with self.lock:
                self.concurrent -= 1


def _packet(sequence: int) -> EncodedMediaPacket:
    return EncodedMediaPacket(
        "video",
        "rtp",
        0,
        float(sequence),
        b"\x80\x60" + sequence.to_bytes(2, "big") + b"\x00" * 8,
        payload_type=96,
        sequence_number=sequence,
        rtp_timestamp=sequence * 90,
        ssrc=1,
    )


def _wait_for(predicate: object) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")
