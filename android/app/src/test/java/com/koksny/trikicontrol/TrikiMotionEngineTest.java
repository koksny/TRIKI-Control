package com.koksny.trikicontrol;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class TrikiMotionEngineTest {
    @Test
    public void leavesCalibrationAfterSettlingWindow() {
        TrikiMotionEngine engine = new TrikiMotionEngine();

        TrikiMotionEngine.State calibrating = engine.addSample(
                0.6,
                sample(0, 0, 0, 0, 0, -2050)
        );
        TrikiMotionEngine.State ready = null;
        for (int index = 0; index < 70; index++) {
            ready = engine.addSample(0.62 + index * 0.02, sample(0, 0, 0, 0, 0, -2050));
        }

        assertEquals(TrikiMotionEngine.Action.SETTLING, calibrating.action);
        assertEquals(TrikiMotionEngine.Action.IDLE, ready.action);
    }

    @Test
    public void settlesThenDetectsAndScalesTwist() {
        TrikiMotionEngine engine = settledEngine();

        TrikiMotionEngine.State low = null;
        for (int index = 0; index < 4; index++) {
            low = engine.addSample(1.5 + index * 0.02, sample(0, 0, 1400, 0, 0, -2050));
        }
        assertTrue(low.action == TrikiMotionEngine.Action.TURN_LEFT
                || low.action == TrikiMotionEngine.Action.TURN_RIGHT);
        double lowStrength = low.strength;

        TrikiMotionEngine.State high = engine.addSample(1.60, sample(0, 0, 2800, 0, 0, -2050));
        assertTrue(high.strength > lowStrength);
    }

    @Test
    public void detectsSingleVerticalStamp() {
        TrikiMotionEngine engine = settledEngine();

        TrikiMotionEngine.State stamp = engine.addSample(1.5, sample(0, 0, 0, 0, 0, -2600));
        TrikiMotionEngine.State repeated = engine.addSample(1.52, sample(0, 0, 0, 0, 0, -2600));

        assertEquals(TrikiMotionEngine.Action.STAMP, stamp.action);
        assertTrue(repeated.action != TrikiMotionEngine.Action.STAMP);
    }

    @Test
    public void rejectsCorruptGravityPacket() {
        TrikiMotionEngine engine = settledEngine();

        TrikiMotionEngine.State state = engine.addSample(1.5, sample(5000, 5000, 5000, 0, 0, 0));

        assertTrue(!state.valid);
        assertEquals(TrikiMotionEngine.Action.IDLE, state.action);
    }

    private static TrikiMotionEngine settledEngine() {
        TrikiMotionEngine engine = new TrikiMotionEngine();
        for (int index = 0; index < 70; index++) {
            engine.addSample(index * 0.02, sample(0, 0, 0, 0, 0, -2050));
        }
        return engine;
    }

    private static MotionSample sample(int ga, int gb, int gc, int ad, int ae, int af) {
        return new MotionSample(0, new int[]{ga, gb, gc, ad, ae, af});
    }
}
