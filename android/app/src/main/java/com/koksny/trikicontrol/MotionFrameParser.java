package com.koksny.trikicontrol;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

final class MotionFrameParser {
    static final int FRAME_TYPE = 0x22;
    static final int FRAME_SIZE = 14;
    static final int MAX_PACKET_ID = 0x0f;

    private byte[] buffer = new byte[0];

    synchronized List<MotionSample> feed(byte[] payload) {
        byte[] joined = Arrays.copyOf(buffer, buffer.length + payload.length);
        System.arraycopy(payload, 0, joined, buffer.length, payload.length);
        buffer = joined;

        List<MotionSample> samples = new ArrayList<>();
        while (true) {
            int marker = findFrameStart();
            if (marker < 0) {
                keepPossibleMarker();
                return samples;
            }
            if (marker > 0) {
                buffer = Arrays.copyOfRange(buffer, marker, buffer.length);
            }
            if (buffer.length < FRAME_SIZE) {
                return samples;
            }

            samples.add(decode(Arrays.copyOf(buffer, FRAME_SIZE)));
            buffer = Arrays.copyOfRange(buffer, FRAME_SIZE, buffer.length);
        }
    }

    synchronized void reset() {
        buffer = new byte[0];
    }

    private int findFrameStart() {
        for (int index = 0; index + 1 < buffer.length; index++) {
            if (unsigned(buffer[index]) == FRAME_TYPE
                    && unsigned(buffer[index + 1]) <= MAX_PACKET_ID) {
                return index;
            }
        }
        if (buffer.length == 1 && unsigned(buffer[0]) == FRAME_TYPE) {
            return 0;
        }
        return -1;
    }

    private void keepPossibleMarker() {
        if (buffer.length > 0 && unsigned(buffer[buffer.length - 1]) == FRAME_TYPE) {
            buffer = new byte[]{(byte) FRAME_TYPE};
        } else {
            buffer = new byte[0];
        }
    }

    private static MotionSample decode(byte[] frame) {
        if (frame.length != FRAME_SIZE
                || unsigned(frame[0]) != FRAME_TYPE
                || unsigned(frame[1]) > MAX_PACKET_ID) {
            throw new IllegalArgumentException("Invalid TRIKI motion frame");
        }
        int[] values = new int[6];
        for (int index = 0; index < values.length; index++) {
            int offset = 2 + index * 2;
            values[index] = (short) (unsigned(frame[offset]) | (unsigned(frame[offset + 1]) << 8));
        }
        return new MotionSample(unsigned(frame[1]), values);
    }

    private static int unsigned(byte value) {
        return value & 0xff;
    }
}
