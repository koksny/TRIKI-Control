package com.koksny.trikicontrol;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONObject;

final class ControlSettings {
    static final String PREFS = "triki_android_settings";

    final double joystickX;
    final double joystickY;
    final double joystickRadius;
    final double stampX;
    final double stampY;
    final double scrubX;
    final double scrubY;
    final double flipX;
    final double flipY;
    final double turnThreshold;
    final double turnSensitivity;
    final boolean invertTurn;
    final ActionBindings bindings;
    final boolean outputEnabled;
    final boolean disclosureAccepted;

    private ControlSettings(SharedPreferences preferences) {
        joystickX = readDouble(preferences, "joystick_x", 0.18, 0.02, 0.98);
        joystickY = readDouble(preferences, "joystick_y", 0.74, 0.02, 0.98);
        joystickRadius = readDouble(preferences, "joystick_radius", 0.12, 0.02, 0.35);
        stampX = readDouble(preferences, "stamp_x", 0.82, 0.02, 0.98);
        stampY = readDouble(preferences, "stamp_y", 0.68, 0.02, 0.98);
        scrubX = readDouble(preferences, "scrub_x", 0.74, 0.02, 0.98);
        scrubY = readDouble(preferences, "scrub_y", 0.84, 0.02, 0.98);
        flipX = readDouble(preferences, "flip_x", 0.90, 0.02, 0.98);
        flipY = readDouble(preferences, "flip_y", 0.84, 0.02, 0.98);
        turnThreshold = readDouble(preferences, "turn_threshold", 1000.0, 400.0, 1600.0);
        turnSensitivity = readDouble(preferences, "turn_sensitivity", 50.0, 0.0, 100.0);
        invertTurn = preferences.getBoolean("invert_turn", true);
        bindings = new ActionBindings(
                readAction(preferences, "bind_turn_left", TrikiMotionEngine.Action.TURN_LEFT),
                readAction(preferences, "bind_turn_right", TrikiMotionEngine.Action.TURN_RIGHT),
                readAction(preferences, "bind_go", TrikiMotionEngine.Action.GO),
                readAction(preferences, "bind_stamp", TrikiMotionEngine.Action.STAMP),
                readAction(preferences, "bind_scrub", TrikiMotionEngine.Action.SCRUB),
                readAction(preferences, "bind_flip", TrikiMotionEngine.Action.FLIP)
        );
        outputEnabled = preferences.getBoolean("output_enabled", false);
        disclosureAccepted = preferences.getBoolean("disclosure_accepted", false);
    }

    static ControlSettings load(Context context) {
        return new ControlSettings(context.getSharedPreferences(PREFS, Context.MODE_PRIVATE));
    }

    static ControlSettings save(Context context, JSONObject json) {
        SharedPreferences preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        SharedPreferences.Editor editor = preferences.edit();
        putDouble(editor, "joystick_x", clamp(json.optDouble("joystickX", 0.18), 0.02, 0.98));
        putDouble(editor, "joystick_y", clamp(json.optDouble("joystickY", 0.74), 0.02, 0.98));
        putDouble(editor, "joystick_radius", clamp(json.optDouble("joystickRadius", 0.12), 0.02, 0.35));
        putDouble(editor, "stamp_x", clamp(json.optDouble("stampX", 0.82), 0.02, 0.98));
        putDouble(editor, "stamp_y", clamp(json.optDouble("stampY", 0.68), 0.02, 0.98));
        putDouble(editor, "scrub_x", clamp(json.optDouble("scrubX", 0.74), 0.02, 0.98));
        putDouble(editor, "scrub_y", clamp(json.optDouble("scrubY", 0.84), 0.02, 0.98));
        putDouble(editor, "flip_x", clamp(json.optDouble("flipX", 0.90), 0.02, 0.98));
        putDouble(editor, "flip_y", clamp(json.optDouble("flipY", 0.84), 0.02, 0.98));
        putDouble(editor, "turn_threshold", clamp(json.optDouble("turnThreshold", 1000.0), 400.0, 1600.0));
        putDouble(editor, "turn_sensitivity", clamp(json.optDouble("turnSensitivity", 50.0), 0.0, 100.0));
        editor.putBoolean("invert_turn", json.optBoolean("invertTurn", true));
        putAction(editor, "bind_turn_left", json.optString("turnLeftAction"), TrikiMotionEngine.Action.TURN_LEFT);
        putAction(editor, "bind_turn_right", json.optString("turnRightAction"), TrikiMotionEngine.Action.TURN_RIGHT);
        putAction(editor, "bind_go", json.optString("goAction"), TrikiMotionEngine.Action.GO);
        putAction(editor, "bind_stamp", json.optString("stampAction"), TrikiMotionEngine.Action.STAMP);
        putAction(editor, "bind_scrub", json.optString("scrubAction"), TrikiMotionEngine.Action.SCRUB);
        putAction(editor, "bind_flip", json.optString("flipAction"), TrikiMotionEngine.Action.FLIP);
        editor.apply();
        return load(context);
    }

    static void setOutputEnabled(Context context, boolean enabled) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putBoolean("output_enabled", enabled)
                .apply();
    }

    static void acceptDisclosure(Context context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putBoolean("disclosure_accepted", true)
                .apply();
    }

    JSONObject toJson() {
        JSONObject json = new JSONObject();
        try {
            json.put("joystickX", joystickX);
            json.put("joystickY", joystickY);
            json.put("joystickRadius", joystickRadius);
            json.put("stampX", stampX);
            json.put("stampY", stampY);
            json.put("scrubX", scrubX);
            json.put("scrubY", scrubY);
            json.put("flipX", flipX);
            json.put("flipY", flipY);
            json.put("turnThreshold", turnThreshold);
            json.put("turnSensitivity", turnSensitivity);
            json.put("invertTurn", invertTurn);
            json.put("turnLeftAction", bindings.turnLeft.name());
            json.put("turnRightAction", bindings.turnRight.name());
            json.put("goAction", bindings.go.name());
            json.put("stampAction", bindings.stamp.name());
            json.put("scrubAction", bindings.scrub.name());
            json.put("flipAction", bindings.flip.name());
            json.put("outputEnabled", outputEnabled);
            json.put("disclosureAccepted", disclosureAccepted);
        } catch (Exception ignored) {
            // JSONObject writes cannot fail for these primitive values.
        }
        return json;
    }

    private static double readDouble(
            SharedPreferences preferences,
            String key,
            double fallback,
            double minimum,
            double maximum
    ) {
        return clamp(Double.longBitsToDouble(preferences.getLong(
                key,
                Double.doubleToRawLongBits(fallback)
        )), minimum, maximum);
    }

    private static void putDouble(SharedPreferences.Editor editor, String key, double value) {
        editor.putLong(key, Double.doubleToRawLongBits(value));
    }

    private static TrikiMotionEngine.Action readAction(
            SharedPreferences preferences,
            String key,
            TrikiMotionEngine.Action fallback
    ) {
        return ActionBindings.parse(preferences.getString(key, fallback.name()), fallback);
    }

    private static void putAction(
            SharedPreferences.Editor editor,
            String key,
            String value,
            TrikiMotionEngine.Action fallback
    ) {
        editor.putString(key, ActionBindings.parse(value, fallback).name());
    }

    private static double clamp(double value, double minimum, double maximum) {
        if (!Double.isFinite(value)) {
            return minimum;
        }
        return Math.max(minimum, Math.min(maximum, value));
    }
}
