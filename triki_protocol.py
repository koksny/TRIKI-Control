from __future__ import annotations

from dataclasses import dataclass


MOTION_FRAME_TYPE = 0x22
MOTION_FRAME_SIZE = 14
MAX_PACKET_ID = 0x0F


@dataclass(frozen=True)
class MotionSample:
    packet_id: int
    values: tuple[int, int, int, int, int, int]


class MotionStreamParser:
    """Incrementally decode TRIKI UART notification bytes into motion samples."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, payload: bytes) -> list[MotionSample]:
        self._buffer.extend(payload)
        samples: list[MotionSample] = []

        while True:
            marker_at = self._find_motion_frame_start()
            if marker_at < 0:
                self._keep_possible_marker_prefix()
                return samples

            if marker_at:
                del self._buffer[:marker_at]

            if len(self._buffer) < MOTION_FRAME_SIZE:
                return samples

            frame = bytes(self._buffer[:MOTION_FRAME_SIZE])
            del self._buffer[:MOTION_FRAME_SIZE]
            samples.append(_decode_motion_frame(frame))

    def _keep_possible_marker_prefix(self) -> None:
        if self._buffer.endswith(bytes([MOTION_FRAME_TYPE])):
            self._buffer[:] = bytes([MOTION_FRAME_TYPE])
        else:
            self._buffer.clear()

    def _find_motion_frame_start(self) -> int:
        for index in range(len(self._buffer) - 1):
            if (
                self._buffer[index] == MOTION_FRAME_TYPE
                and self._buffer[index + 1] <= MAX_PACKET_ID
            ):
                return index
        if len(self._buffer) == 1 and self._buffer[0] == MOTION_FRAME_TYPE:
            return 0
        return -1


def _decode_motion_frame(frame: bytes) -> MotionSample:
    if (
        len(frame) != MOTION_FRAME_SIZE
        or frame[0] != MOTION_FRAME_TYPE
        or frame[1] > MAX_PACKET_ID
    ):
        raise ValueError("invalid TRIKI motion frame")

    packet_id = frame[1]
    values = tuple(
        int.from_bytes(frame[offset : offset + 2], "little", signed=True)
        for offset in range(2, MOTION_FRAME_SIZE, 2)
    )
    return MotionSample(packet_id=packet_id, values=values)  # type: ignore[arg-type]
