package com.koksny.trikicontrol;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.ComponentName;
import android.content.Context;
import android.graphics.Path;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.DisplayMetrics;
import android.view.accessibility.AccessibilityEvent;

public final class TrikiAccessibilityService extends AccessibilityService {
    private static final long SEGMENT_MS = 90L;
    private static final long SCROLL_SLOW_MS = 340L;
    private static final long SCROLL_FAST_MS = 150L;
    private static volatile TrikiAccessibilityService instance;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TrikiMotionEngine.Action desiredAction = TrikiMotionEngine.Action.IDLE;
    private double desiredStrength;
    private TrikiMotionEngine.Action pendingTap = TrikiMotionEngine.Action.IDLE;
    private GestureDescription.StrokeDescription activeStroke;
    private float lastX;
    private float lastY;
    private boolean dispatchInFlight;
    private boolean activeFlipStroke;
    private int testGeneration;

    static boolean isRunning() {
        return instance != null;
    }

    static boolean isEnabled(Context context) {
        String expected = new ComponentName(context, TrikiAccessibilityService.class).flattenToString();
        String enabled = Settings.Secure.getString(
                context.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        );
        if (enabled == null) {
            return false;
        }
        TextUtils.SimpleStringSplitter splitter = new TextUtils.SimpleStringSplitter(':');
        splitter.setString(enabled);
        while (splitter.hasNext()) {
            if (expected.equalsIgnoreCase(splitter.next())) {
                return true;
            }
        }
        return false;
    }

    static boolean applyMotion(TrikiMotionEngine.Action action, double strength) {
        TrikiAccessibilityService service = instance;
        if (service == null) {
            return false;
        }
        service.handler.post(() -> service.setDesiredMotion(action, strength));
        return true;
    }

    static boolean tap(TrikiMotionEngine.Action action) {
        TrikiAccessibilityService service = instance;
        if (service == null) {
            return false;
        }
        service.handler.post(() -> service.requestTap(action));
        return true;
    }

    static boolean runDelayedTest(
            TrikiMotionEngine.Action action,
            long delayMs,
            long durationMs
    ) {
        TrikiAccessibilityService service = instance;
        if (service == null) {
            return false;
        }
        int generation = ++service.testGeneration;
        service.handler.postDelayed(() -> {
            if (generation != service.testGeneration) {
                return;
            }
            if (action == TrikiMotionEngine.Action.STAMP || action == TrikiMotionEngine.Action.SCRUB) {
                service.requestTap(action);
                return;
            }
            service.setDesiredMotion(action, 0.85);
            service.handler.postDelayed(() -> {
                if (generation == service.testGeneration) {
                    service.setDesiredMotion(TrikiMotionEngine.Action.IDLE, 0.0);
                }
            }, Math.max(250L, durationMs));
        }, Math.max(0L, delayMs));
        return true;
    }

    static void releaseAll() {
        TrikiAccessibilityService service = instance;
        if (service != null) {
            service.handler.post(service::cancelAll);
        }
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // No app content or event payload is inspected.
    }

    @Override
    public void onInterrupt() {
        handler.post(this::cancelAll);
    }

    @Override
    public boolean onUnbind(android.content.Intent intent) {
        handler.post(this::cancelAll);
        ControlSettings.setOutputEnabled(this, false);
        TrikiControlService.reloadSettings();
        instance = null;
        return super.onUnbind(intent);
    }

    private void setDesiredMotion(TrikiMotionEngine.Action action, double strength) {
        if (!isMotionOutput(action)) {
            action = TrikiMotionEngine.Action.IDLE;
        }
        desiredAction = action;
        desiredStrength = Math.max(0.0, Math.min(1.0, strength));
        pumpGesture();
    }

    private void cancelAll() {
        testGeneration++;
        pendingTap = TrikiMotionEngine.Action.IDLE;
        desiredAction = TrikiMotionEngine.Action.IDLE;
        desiredStrength = 0.0;
        pumpGesture();
    }

    private void requestTap(TrikiMotionEngine.Action action) {
        if (action != TrikiMotionEngine.Action.STAMP && action != TrikiMotionEngine.Action.SCRUB) {
            return;
        }
        pendingTap = action;
        desiredAction = TrikiMotionEngine.Action.IDLE;
        desiredStrength = 0.0;
        pumpGesture();
    }

    private void pumpGesture() {
        if (dispatchInFlight) {
            return;
        }
        if (activeStroke != null) {
            continueOrRelease();
            return;
        }
        if (pendingTap != TrikiMotionEngine.Action.IDLE) {
            dispatchTap();
            return;
        }
        if (desiredAction != TrikiMotionEngine.Action.IDLE) {
            if (isScroll(desiredAction)) {
                dispatchScroll();
            } else {
                startContinuousStroke();
            }
        }
    }

    private void startContinuousStroke() {
        Point center = joystickCenter();
        Point target = targetFor(desiredAction, desiredStrength);
        Path path = new Path();
        if (desiredAction == TrikiMotionEngine.Action.FLIP) {
            path.moveTo(target.x, target.y);
        } else {
            path.moveTo(center.x, center.y);
            path.lineTo(target.x, target.y);
        }
        GestureDescription.StrokeDescription stroke = new GestureDescription.StrokeDescription(
                path,
                0,
                SEGMENT_MS,
                true
        );
        dispatchStroke(stroke, target.x, target.y, true, desiredAction == TrikiMotionEngine.Action.FLIP);
    }

    private void continueOrRelease() {
        boolean keepHolding = isHeldStroke(desiredAction)
                && (desiredAction == TrikiMotionEngine.Action.FLIP) == activeFlipStroke;
        Point target = keepHolding ? targetFor(desiredAction, desiredStrength) : new Point(lastX, lastY);
        Path path = new Path();
        path.moveTo(lastX, lastY);
        if (Math.abs(target.x - lastX) > 0.01f || Math.abs(target.y - lastY) > 0.01f) {
            path.lineTo(target.x, target.y);
        } else {
            DisplayMetrics metrics = getResources().getDisplayMetrics();
            float nudgeX = lastX < metrics.widthPixels - 2.0f ? lastX + 1.0f : lastX - 1.0f;
            target = bounded(nudgeX, lastY, metrics.widthPixels, metrics.heightPixels);
            path.lineTo(target.x, target.y);
        }
        GestureDescription.StrokeDescription stroke = activeStroke.continueStroke(
                path,
                0,
                keepHolding ? SEGMENT_MS : 1L,
                keepHolding
        );
        dispatchStroke(stroke, target.x, target.y, keepHolding, activeFlipStroke);
    }

    private void dispatchStroke(
            GestureDescription.StrokeDescription stroke,
            float targetX,
            float targetY,
            boolean continues,
            boolean flipStroke
    ) {
        GestureDescription gesture = new GestureDescription.Builder().addStroke(stroke).build();
        dispatchInFlight = true;
        boolean accepted = dispatchGesture(gesture, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription gestureDescription) {
                dispatchInFlight = false;
                activeStroke = continues ? stroke : null;
                activeFlipStroke = continues && flipStroke;
                lastX = targetX;
                lastY = targetY;
                handler.post(TrikiAccessibilityService.this::pumpGesture);
            }

            @Override
            public void onCancelled(GestureDescription gestureDescription) {
                dispatchInFlight = false;
                activeStroke = null;
                activeFlipStroke = false;
                handler.postDelayed(TrikiAccessibilityService.this::pumpGesture, SEGMENT_MS);
            }
        }, handler);
        if (!accepted) {
            dispatchInFlight = false;
            activeStroke = null;
            activeFlipStroke = false;
            handler.postDelayed(this::pumpGesture, SEGMENT_MS);
        }
    }

    private void dispatchTap() {
        TrikiMotionEngine.Action action = pendingTap;
        pendingTap = TrikiMotionEngine.Action.IDLE;
        Point point = action == TrikiMotionEngine.Action.SCRUB ? scrubPoint() : stampPoint();
        Path path = new Path();
        path.moveTo(point.x, point.y);
        GestureDescription.StrokeDescription stroke = new GestureDescription.StrokeDescription(path, 0, 65L);
        GestureDescription gesture = new GestureDescription.Builder().addStroke(stroke).build();
        dispatchInFlight = true;
        boolean accepted = dispatchGesture(gesture, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription gestureDescription) {
                dispatchInFlight = false;
                handler.post(TrikiAccessibilityService.this::pumpGesture);
            }

            @Override
            public void onCancelled(GestureDescription gestureDescription) {
                dispatchInFlight = false;
                handler.post(TrikiAccessibilityService.this::pumpGesture);
            }
        }, handler);
        if (!accepted) {
            dispatchInFlight = false;
            handler.post(this::pumpGesture);
        }
    }

    private void dispatchScroll() {
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        float width = metrics.widthPixels;
        float height = metrics.heightPixels;
        float strength = (float) desiredStrength;
        float travel = height * (0.20f + 0.28f * strength);
        float centerY = height * 0.50f;
        float startY = centerY + travel * 0.5f;
        float endY = centerY - travel * 0.5f;
        if (desiredAction == TrikiMotionEngine.Action.SCROLL_UP) {
            float swap = startY;
            startY = endY;
            endY = swap;
        }
        Point start = bounded(width * 0.5f, startY, width, height);
        Point end = bounded(width * 0.5f, endY, width, height);
        Path path = new Path();
        path.moveTo(start.x, start.y);
        path.lineTo(end.x, end.y);
        long duration = Math.round(SCROLL_SLOW_MS
                - (SCROLL_SLOW_MS - SCROLL_FAST_MS) * strength);
        GestureDescription.StrokeDescription stroke = new GestureDescription.StrokeDescription(
                path,
                0,
                duration
        );
        dispatchStroke(stroke, end.x, end.y, false, false);
    }

    private Point targetFor(TrikiMotionEngine.Action action, double strength) {
        ControlSettings settings = ControlSettings.load(this);
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        float width = metrics.widthPixels;
        float height = metrics.heightPixels;
        float radius = (float) (settings.joystickRadius * Math.min(width, height));
        Point center = joystickCenter();
        if (action == TrikiMotionEngine.Action.TURN_LEFT) {
            return bounded(center.x - radius * (float) strength, center.y, width, height);
        }
        if (action == TrikiMotionEngine.Action.TURN_RIGHT) {
            return bounded(center.x + radius * (float) strength, center.y, width, height);
        }
        if (action == TrikiMotionEngine.Action.GO) {
            return bounded(center.x, center.y - radius * (float) strength, width, height);
        }
        if (action == TrikiMotionEngine.Action.JOYSTICK_DOWN) {
            return bounded(center.x, center.y + radius * (float) strength, width, height);
        }
        if (action == TrikiMotionEngine.Action.FLIP) {
            return bounded(
                    (float) (settings.flipX * width),
                    (float) (settings.flipY * height),
                    width,
                    height
            );
        }
        return center;
    }

    private Point joystickCenter() {
        ControlSettings settings = ControlSettings.load(this);
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        return bounded(
                (float) (settings.joystickX * metrics.widthPixels),
                (float) (settings.joystickY * metrics.heightPixels),
                metrics.widthPixels,
                metrics.heightPixels
        );
    }

    private Point stampPoint() {
        ControlSettings settings = ControlSettings.load(this);
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        return bounded(
                (float) (settings.stampX * metrics.widthPixels),
                (float) (settings.stampY * metrics.heightPixels),
                metrics.widthPixels,
                metrics.heightPixels
        );
    }

    private Point scrubPoint() {
        ControlSettings settings = ControlSettings.load(this);
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        return bounded(
                (float) (settings.scrubX * metrics.widthPixels),
                (float) (settings.scrubY * metrics.heightPixels),
                metrics.widthPixels,
                metrics.heightPixels
        );
    }

    private static Point bounded(float x, float y, float width, float height) {
        return new Point(
                Math.max(1.0f, Math.min(width - 2.0f, x)),
                Math.max(1.0f, Math.min(height - 2.0f, y))
        );
    }

    private static boolean isMotionOutput(TrikiMotionEngine.Action action) {
        return isHeldStroke(action) || isScroll(action);
    }

    private static boolean isHeldStroke(TrikiMotionEngine.Action action) {
        return action == TrikiMotionEngine.Action.TURN_LEFT
                || action == TrikiMotionEngine.Action.TURN_RIGHT
                || action == TrikiMotionEngine.Action.GO
                || action == TrikiMotionEngine.Action.JOYSTICK_DOWN
                || action == TrikiMotionEngine.Action.FLIP;
    }

    private static boolean isScroll(TrikiMotionEngine.Action action) {
        return action == TrikiMotionEngine.Action.SCROLL_UP
                || action == TrikiMotionEngine.Action.SCROLL_DOWN;
    }

    private static final class Point {
        final float x;
        final float y;

        Point(float x, float y) {
            this.x = x;
            this.y = y;
        }
    }
}
