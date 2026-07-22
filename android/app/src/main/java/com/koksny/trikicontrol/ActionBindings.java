package com.koksny.trikicontrol;

final class ActionBindings {
    final TrikiMotionEngine.Action turnLeft;
    final TrikiMotionEngine.Action turnRight;
    final TrikiMotionEngine.Action go;
    final TrikiMotionEngine.Action stamp;
    final TrikiMotionEngine.Action scrub;
    final TrikiMotionEngine.Action flip;

    ActionBindings(
            TrikiMotionEngine.Action turnLeft,
            TrikiMotionEngine.Action turnRight,
            TrikiMotionEngine.Action go,
            TrikiMotionEngine.Action stamp,
            TrikiMotionEngine.Action scrub,
            TrikiMotionEngine.Action flip
    ) {
        this.turnLeft = outputOrDefault(turnLeft, TrikiMotionEngine.Action.TURN_LEFT);
        this.turnRight = outputOrDefault(turnRight, TrikiMotionEngine.Action.TURN_RIGHT);
        this.go = outputOrDefault(go, TrikiMotionEngine.Action.GO);
        this.stamp = outputOrDefault(stamp, TrikiMotionEngine.Action.STAMP);
        this.scrub = outputOrDefault(scrub, TrikiMotionEngine.Action.SCRUB);
        this.flip = outputOrDefault(flip, TrikiMotionEngine.Action.FLIP);
    }

    static ActionBindings defaults() {
        return new ActionBindings(
                TrikiMotionEngine.Action.TURN_LEFT,
                TrikiMotionEngine.Action.TURN_RIGHT,
                TrikiMotionEngine.Action.GO,
                TrikiMotionEngine.Action.STAMP,
                TrikiMotionEngine.Action.SCRUB,
                TrikiMotionEngine.Action.FLIP
        );
    }

    TrikiMotionEngine.Action map(TrikiMotionEngine.Action input) {
        if (input == null) {
            return TrikiMotionEngine.Action.IDLE;
        }
        switch (input) {
            case TURN_LEFT:
                return turnLeft;
            case TURN_RIGHT:
                return turnRight;
            case GO:
                return go;
            case STAMP:
                return stamp;
            case SCRUB:
                return scrub;
            case FLIP:
                return flip;
            case SETTLING:
                return TrikiMotionEngine.Action.SETTLING;
            case IDLE:
            default:
                return TrikiMotionEngine.Action.IDLE;
        }
    }

    static TrikiMotionEngine.Action parse(String value, TrikiMotionEngine.Action fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return outputOrDefault(TrikiMotionEngine.Action.valueOf(value), fallback);
        } catch (IllegalArgumentException exception) {
            return fallback;
        }
    }

    private static TrikiMotionEngine.Action outputOrDefault(
            TrikiMotionEngine.Action value,
            TrikiMotionEngine.Action fallback
    ) {
        if (value == null || value == TrikiMotionEngine.Action.SETTLING) {
            return fallback;
        }
        return value;
    }
}
