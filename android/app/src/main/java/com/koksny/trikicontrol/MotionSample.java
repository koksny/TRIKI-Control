package com.koksny.trikicontrol;

import java.util.Arrays;

final class MotionSample {
    final int packetId;
    final int[] values;

    MotionSample(int packetId, int[] values) {
        if (values.length != 6) {
            throw new IllegalArgumentException("TRIKI samples contain six channels");
        }
        this.packetId = packetId;
        this.values = Arrays.copyOf(values, values.length);
    }
}
