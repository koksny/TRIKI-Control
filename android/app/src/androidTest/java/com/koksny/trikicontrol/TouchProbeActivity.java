package com.koksny.trikicontrol;

import android.app.Activity;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.ResultReceiver;
import android.util.Log;
import android.view.MotionEvent;
import android.widget.FrameLayout;

public final class TouchProbeActivity extends Activity {
    private static final String TAG = "TrikiTouchProbe";
    static final String EXTRA_RECEIVER = "receiver";
    static final int EVENT_READY = 100;

    private ResultReceiver receiver;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            receiver = getIntent().getParcelableExtra(EXTRA_RECEIVER, ResultReceiver.class);
        } else {
            receiver = getIntent().getParcelableExtra(EXTRA_RECEIVER);
        }

        FrameLayout surface = new FrameLayout(this);
        surface.setBackgroundColor(Color.WHITE);
        setContentView(surface);

        if (receiver != null) {
            receiver.send(EVENT_READY, Bundle.EMPTY);
        }
        Log.i(TAG, "READY");
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent event) {
        Log.i(
                TAG,
                MotionEvent.actionToString(event.getActionMasked())
                        + " x=" + event.getX()
                        + " y=" + event.getY()
        );
        if (receiver != null) {
            Bundle coordinates = new Bundle();
            coordinates.putFloat("x", event.getX());
            coordinates.putFloat("y", event.getY());
            receiver.send(event.getActionMasked(), coordinates);
        }
        return true;
    }
}
