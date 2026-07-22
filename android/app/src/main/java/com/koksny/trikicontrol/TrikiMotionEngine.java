package com.koksny.trikicontrol;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

final class TrikiMotionEngine {
    enum Action {
        IDLE,
        SETTLING,
        TURN_LEFT,
        TURN_RIGHT,
        GO,
        JOYSTICK_DOWN,
        SCROLL_UP,
        SCROLL_DOWN,
        STAMP,
        FLIP,
        SCRUB
    }

    static final class State {
        final Action action;
        final double strength;
        final double twist;
        final double spin;
        final double tilt;
        final boolean valid;

        State(Action action, double strength, double twist, double spin, double tilt, boolean valid) {
            this.action = action;
            this.strength = strength;
            this.twist = twist;
            this.spin = spin;
            this.tilt = tilt;
            this.valid = valid;
        }
    }

    private static final double GRAVITY = 2050.0;
    private static final double SETTLE_SECONDS = 1.2;

    private double turnThreshold = 1000.0;
    private double turnSensitivity = 50.0;
    private boolean invertTurn = true;

    private double[] gravity;
    private double[] gravityReference;
    private double[] gyroBias;
    private double firstTime;
    private boolean booted;
    private final List<double[]> bootGyro = new ArrayList<>();
    private final List<double[]> bootAccel = new ArrayList<>();
    private Double stillSince;
    private Double flipSince;
    private boolean flipped;
    private Double lastStamp;
    private boolean stampArmed = true;
    private int goTiltSamples;
    private int turning;
    private boolean horizontalActive;
    private Action gesture = Action.IDLE;
    private Action candidate = Action.IDLE;
    private int candidateSamples;
    private int idleSamples;
    private double heldStrength;

    synchronized void configure(double threshold, double sensitivity, boolean inverted) {
        turnThreshold = clamp(threshold, 400.0, 1600.0);
        turnSensitivity = clamp(sensitivity, 0.0, 100.0);
        invertTurn = inverted;
    }

    synchronized void reset() {
        gravity = null;
        gravityReference = null;
        gyroBias = null;
        booted = false;
        bootGyro.clear();
        bootAccel.clear();
        stillSince = null;
        flipSince = null;
        flipped = false;
        lastStamp = null;
        stampArmed = true;
        goTiltSamples = 0;
        turning = 0;
        horizontalActive = false;
        gesture = Action.IDLE;
        candidate = Action.IDLE;
        candidateSamples = 0;
        idleSamples = 0;
        heldStrength = 0.0;
    }

    synchronized State addSample(double elapsedSeconds, MotionSample sample) {
        int[] value = sample.values;
        double[] gyro = {value[0], value[1], value[2]};
        double[] accel = {value[3], value[4], value[5]};
        double accelMagnitude = norm(accel);
        if (accelMagnitude < 800.0 || accelMagnitude > 8000.0) {
            return new State(Action.IDLE, 0.0, 0.0, 0.0, 0.0, false);
        }

        if (gravity == null) {
            gravity = Arrays.copyOf(accel, 3);
            gravityReference = Arrays.copyOf(accel, 3);
            gyroBias = Arrays.copyOf(gyro, 3);
            firstTime = elapsedSeconds;
        }

        if (!booted) {
            bootGyro.add(Arrays.copyOf(gyro, 3));
            bootAccel.add(Arrays.copyOf(accel, 3));
            if (elapsedSeconds - firstTime >= SETTLE_SECONDS && bootGyro.size() >= 5) {
                for (int axis = 0; axis < 3; axis++) {
                    gyroBias[axis] = median(bootGyro, axis);
                    gravityReference[axis] = median(bootAccel, axis);
                    gravity[axis] = gravityReference[axis];
                }
                bootGyro.clear();
                bootAccel.clear();
                booted = true;
            }
        }

        double accelDeviation = Math.abs(accelMagnitude - GRAVITY);
        double gravityAlpha = accelDeviation < 400.0 ? 0.25 : 0.0;
        for (int axis = 0; axis < 3; axis++) {
            gravity[axis] = (1.0 - gravityAlpha) * gravity[axis] + gravityAlpha * accel[axis];
        }
        double[] gravityUnit = unit(gravity);
        double[] correctedGyro = {
                gyro[0] - gyroBias[0],
                gyro[1] - gyroBias[1],
                gyro[2] - gyroBias[2]
        };
        double spin = norm(correctedGyro);
        double twist = dot(correctedGyro, gravityUnit);
        double verticalImpact = Math.abs(Math.abs(dot(accel, gravityUnit)) - GRAVITY);
        double horizontalD = accel[0] - gravityReference[0];
        double horizontalE = accel[1] - gravityReference[1];
        double horizontalMagnitude = Math.hypot(horizontalD, horizontalE);
        double tilt = Math.sqrt(Math.max(0.0, GRAVITY * GRAVITY - gravity[2] * gravity[2]));

        if (tilt >= 200.0) {
            goTiltSamples = Math.min(goTiltSamples + 1, 6);
        } else {
            goTiltSamples = Math.max(goTiltSamples - 1, 0);
        }
        boolean tilted = goTiltSamples >= 2;

        boolean invertedRest = gravity[2] >= 1200.0 && accelDeviation < 90.0 && spin < 400.0;
        if (invertedRest) {
            if (flipSince == null) {
                flipSince = elapsedSeconds;
            }
            flipped = elapsedSeconds - flipSince >= 0.30;
        } else {
            flipSince = null;
            flipped = false;
        }

        boolean motionStill = spin < 220.0 && accelDeviation < 90.0;
        if (motionStill) {
            if (stillSince == null) {
                stillSince = elapsedSeconds;
            }
            if (elapsedSeconds - stillSince >= 0.20) {
                if (!flipped && tilt < 160.0) {
                    gravityReference[0] = 0.88 * gravityReference[0] + 0.12 * accel[0];
                    gravityReference[1] = 0.88 * gravityReference[1] + 0.12 * accel[1];
                }
                for (int axis = 0; axis < 3; axis++) {
                    gyroBias[axis] = 0.95 * gyroBias[axis] + 0.05 * gyro[axis];
                }
            }
        } else {
            stillSince = null;
        }

        if (!booted || elapsedSeconds - firstTime < SETTLE_SECONDS) {
            clearGesture();
            return new State(Action.SETTLING, 0.0, twist, spin, tilt, true);
        }

        if (flipped) {
            clearGesture();
            return new State(Action.FLIP, 1.0, twist, spin, tilt, true);
        }

        if (verticalImpact < 190.0) {
            stampArmed = true;
        }
        boolean stampReady = lastStamp == null || elapsedSeconds - lastStamp >= 0.45;
        if (verticalImpact >= 380.0 && spin < 800.0 && tilt < 550.0 && stampArmed && stampReady) {
            lastStamp = elapsedSeconds;
            stampArmed = false;
            goTiltSamples = 0;
            clearGesture();
            return new State(Action.STAMP, 1.0, twist, spin, tilt, true);
        }

        Action raw = rawCandidate(twist, spin, horizontalMagnitude, tilted, accelDeviation);
        double rawStrength = strength(raw, twist, tilt);
        Action active = lockCandidate(raw, rawStrength);
        return new State(active, active == Action.IDLE ? 0.0 : heldStrength, twist, spin, tilt, true);
    }

    private Action rawCandidate(
            double twist,
            double spin,
            double horizontalMagnitude,
            boolean tilted,
            double accelDeviation
    ) {
        double activeThreshold = horizontalActive ? 160.0 : 270.0;
        horizontalActive = horizontalMagnitude >= activeThreshold;
        if (tilted && horizontalActive) {
            turning = 0;
            return Action.GO;
        }

        double spinThreshold = 1300.0 - turnSensitivity * 10.0;
        double axisFraction = 0.75 - turnSensitivity * 0.004;
        double threshold = turning == 0 ? turnThreshold : turnThreshold * 0.69;
        if (spin >= spinThreshold && Math.abs(twist) >= threshold
                && Math.abs(twist) >= axisFraction * spin) {
            turning = twist > 0.0 ? 1 : -1;
            boolean right = twist > 0.0;
            if (invertTurn) {
                right = !right;
            }
            return right ? Action.TURN_RIGHT : Action.TURN_LEFT;
        }
        turning = 0;
        if (!horizontalActive) {
            return Action.IDLE;
        }
        if (spin < 800.0 && accelDeviation < 450.0) {
            return Action.SCRUB;
        }
        return Action.IDLE;
    }

    private Action lockCandidate(Action raw, double rawStrength) {
        if (gesture != Action.IDLE) {
            if (raw == gesture) {
                idleSamples = 0;
                candidate = Action.IDLE;
                candidateSamples = 0;
                heldStrength = rawStrength;
                return gesture;
            }
            if (raw == Action.IDLE) {
                candidate = Action.IDLE;
                candidateSamples = 0;
                idleSamples++;
                if (idleSamples >= 3) {
                    clearGesture();
                    return Action.IDLE;
                }
                return gesture;
            }
            idleSamples = 0;
            if (candidate == raw) {
                candidateSamples++;
            } else {
                candidate = raw;
                candidateSamples = 1;
            }
            if (candidateSamples >= engageSamples(raw)) {
                gesture = raw;
                candidate = Action.IDLE;
                candidateSamples = 0;
                heldStrength = rawStrength;
            }
            return gesture;
        }

        if (raw == Action.IDLE) {
            candidate = Action.IDLE;
            candidateSamples = 0;
            return Action.IDLE;
        }
        if (candidate == raw) {
            candidateSamples++;
        } else {
            candidate = raw;
            candidateSamples = 1;
        }
        if (candidateSamples >= engageSamples(raw)) {
            gesture = raw;
            candidate = Action.IDLE;
            candidateSamples = 0;
            idleSamples = 0;
            heldStrength = rawStrength;
            return gesture;
        }
        return Action.IDLE;
    }

    private static int engageSamples(Action action) {
        return action == Action.SCRUB ? 4 : 3;
    }

    private double strength(Action action, double twist, double tilt) {
        if (action == Action.TURN_LEFT || action == Action.TURN_RIGHT) {
            double release = turnThreshold * 0.69;
            double full = turnThreshold * 2.6;
            return clamp((Math.abs(twist) - release) / (full - release), 0.18, 1.0);
        }
        if (action == Action.GO) {
            return clamp((tilt - 200.0) / 900.0, 0.35, 1.0);
        }
        return action == Action.IDLE ? 0.0 : 1.0;
    }

    private void clearGesture() {
        gesture = Action.IDLE;
        candidate = Action.IDLE;
        candidateSamples = 0;
        idleSamples = 0;
        heldStrength = 0.0;
        turning = 0;
        horizontalActive = false;
    }

    private static double median(List<double[]> samples, int axis) {
        List<Double> values = new ArrayList<>(samples.size());
        for (double[] sample : samples) {
            values.add(sample[axis]);
        }
        Collections.sort(values);
        int middle = values.size() / 2;
        if (values.size() % 2 == 1) {
            return values.get(middle);
        }
        return 0.5 * (values.get(middle - 1) + values.get(middle));
    }

    private static double dot(double[] left, double[] right) {
        return left[0] * right[0] + left[1] * right[1] + left[2] * right[2];
    }

    private static double norm(double[] value) {
        return Math.sqrt(dot(value, value));
    }

    private static double[] unit(double[] value) {
        double length = norm(value);
        if (length <= 1e-9) {
            return new double[]{0.0, 0.0, 0.0};
        }
        return new double[]{value[0] / length, value[1] / length, value[2] / length};
    }

    private static double clamp(double value, double minimum, double maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }
}
