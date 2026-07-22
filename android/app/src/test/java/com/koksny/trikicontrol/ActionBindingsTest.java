package com.koksny.trikicontrol;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public final class ActionBindingsTest {
    @Test
    public void defaultsPreservePreBindingBehavior() {
        ActionBindings bindings = ActionBindings.defaults();

        assertEquals(TrikiMotionEngine.Action.TURN_LEFT, bindings.map(TrikiMotionEngine.Action.TURN_LEFT));
        assertEquals(TrikiMotionEngine.Action.TURN_RIGHT, bindings.map(TrikiMotionEngine.Action.TURN_RIGHT));
        assertEquals(TrikiMotionEngine.Action.GO, bindings.map(TrikiMotionEngine.Action.GO));
        assertEquals(TrikiMotionEngine.Action.STAMP, bindings.map(TrikiMotionEngine.Action.STAMP));
        assertEquals(TrikiMotionEngine.Action.SCRUB, bindings.map(TrikiMotionEngine.Action.SCRUB));
        assertEquals(TrikiMotionEngine.Action.FLIP, bindings.map(TrikiMotionEngine.Action.FLIP));
    }

    @Test
    public void mapsEveryRecognizedInputToConfiguredOutput() {
        ActionBindings bindings = new ActionBindings(
                TrikiMotionEngine.Action.SCROLL_DOWN,
                TrikiMotionEngine.Action.SCROLL_UP,
                TrikiMotionEngine.Action.TURN_RIGHT,
                TrikiMotionEngine.Action.FLIP,
                TrikiMotionEngine.Action.GO,
                TrikiMotionEngine.Action.SCRUB
        );

        assertEquals(TrikiMotionEngine.Action.SCROLL_DOWN, bindings.map(TrikiMotionEngine.Action.TURN_LEFT));
        assertEquals(TrikiMotionEngine.Action.SCROLL_UP, bindings.map(TrikiMotionEngine.Action.TURN_RIGHT));
        assertEquals(TrikiMotionEngine.Action.TURN_RIGHT, bindings.map(TrikiMotionEngine.Action.GO));
        assertEquals(TrikiMotionEngine.Action.FLIP, bindings.map(TrikiMotionEngine.Action.STAMP));
        assertEquals(TrikiMotionEngine.Action.GO, bindings.map(TrikiMotionEngine.Action.SCRUB));
        assertEquals(TrikiMotionEngine.Action.SCRUB, bindings.map(TrikiMotionEngine.Action.FLIP));
    }

    @Test
    public void idleAndSettlingRemainLifecycleStates() {
        ActionBindings bindings = ActionBindings.defaults();

        assertEquals(TrikiMotionEngine.Action.IDLE, bindings.map(TrikiMotionEngine.Action.IDLE));
        assertEquals(TrikiMotionEngine.Action.SETTLING, bindings.map(TrikiMotionEngine.Action.SETTLING));
        assertEquals(TrikiMotionEngine.Action.IDLE, bindings.map(null));
    }

    @Test
    public void invalidOrLifecycleOutputFallsBackToDefault() {
        assertEquals(
                TrikiMotionEngine.Action.GO,
                ActionBindings.parse("not-an-action", TrikiMotionEngine.Action.GO)
        );
        assertEquals(
                TrikiMotionEngine.Action.GO,
                ActionBindings.parse("SETTLING", TrikiMotionEngine.Action.GO)
        );
    }
}
