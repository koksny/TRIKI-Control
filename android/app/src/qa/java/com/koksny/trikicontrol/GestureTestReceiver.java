package com.koksny.trikicontrol;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

public final class GestureTestReceiver extends BroadcastReceiver {
    private static final String TAG = "TrikiGestureTest";
    private static final long TEST_DELAY_MS = 250L;

    @Override
    public void onReceive(Context context, Intent intent) {
        String command = intent.getStringExtra("command");
        TrikiMotionEngine.Action action = actionFor(command);
        if (action == null) {
            Log.e(TAG, "Rejected unknown command: " + command);
            setResultCode(2);
            return;
        }

        long durationMs = action == TrikiMotionEngine.Action.STAMP ? 100L : 500L;
        boolean accepted = TrikiAccessibilityService.runDelayedTest(
                action,
                TEST_DELAY_MS,
                durationMs
        );
        Log.i(TAG, "command=" + command + " accepted=" + accepted);
        setResultCode(accepted ? 0 : 1);
    }

    private static TrikiMotionEngine.Action actionFor(String command) {
        if ("tap".equals(command)) {
            return TrikiMotionEngine.Action.STAMP;
        }
        if ("turn_right".equals(command)) {
            return TrikiMotionEngine.Action.TURN_RIGHT;
        }
        if ("down".equals(command)) {
            return TrikiMotionEngine.Action.JOYSTICK_DOWN;
        }
        if ("scroll_up".equals(command)) {
            return TrikiMotionEngine.Action.SCROLL_UP;
        }
        if ("scroll_down".equals(command)) {
            return TrikiMotionEngine.Action.SCROLL_DOWN;
        }
        if ("flip".equals(command)) {
            return TrikiMotionEngine.Action.FLIP;
        }
        return null;
    }
}
