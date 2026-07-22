package com.koksny.trikicontrol;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

import java.util.List;
import org.junit.Test;

public class MotionFrameParserTest {
    @Test
    public void parsesSplitFramesAndSignedValues() {
        MotionFrameParser parser = new MotionFrameParser();
        byte[] frame = frame(7, 12, -31, -22, 24, 0, -2050);

        assertEquals(0, parser.feed(slice(frame, 0, 5)).size());
        List<MotionSample> samples = parser.feed(slice(frame, 5, frame.length));

        assertEquals(1, samples.size());
        assertEquals(7, samples.get(0).packetId);
        assertArrayEquals(new int[]{12, -31, -22, 24, 0, -2050}, samples.get(0).values);
    }

    @Test
    public void skipsNoiseAndParsesConsecutiveFrames() {
        MotionFrameParser parser = new MotionFrameParser();
        byte[] first = frame(1, 1, 2, 3, 4, 5, 6);
        byte[] second = frame(2, -1, -2, -3, -4, -5, -6);
        byte[] payload = new byte[3 + first.length + second.length];
        payload[0] = 0x55;
        payload[1] = 0x22;
        payload[2] = 0x7f;
        System.arraycopy(first, 0, payload, 3, first.length);
        System.arraycopy(second, 0, payload, 3 + first.length, second.length);

        List<MotionSample> samples = parser.feed(payload);

        assertEquals(2, samples.size());
        assertEquals(1, samples.get(0).packetId);
        assertEquals(2, samples.get(1).packetId);
    }

    private static byte[] frame(int packetId, int... values) {
        byte[] frame = new byte[14];
        frame[0] = 0x22;
        frame[1] = (byte) packetId;
        for (int index = 0; index < values.length; index++) {
            frame[2 + index * 2] = (byte) (values[index] & 0xff);
            frame[3 + index * 2] = (byte) ((values[index] >> 8) & 0xff);
        }
        return frame;
    }

    private static byte[] slice(byte[] value, int start, int end) {
        byte[] result = new byte[end - start];
        System.arraycopy(value, start, result, 0, result.length);
        return result;
    }
}
