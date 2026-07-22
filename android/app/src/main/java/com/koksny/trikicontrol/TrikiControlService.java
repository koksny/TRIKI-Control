package com.koksny.trikicontrol;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothStatusCodes;
import android.bluetooth.le.BluetoothLeScanner;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.ParcelUuid;
import android.os.SystemClock;
import java.lang.ref.WeakReference;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import org.json.JSONObject;

@SuppressLint("MissingPermission")
public final class TrikiControlService extends Service {
    interface Listener {
        void onTrikiState(JSONObject state);
    }

    static final String ACTION_CONNECT = "com.koksny.trikicontrol.CONNECT";
    static final String ACTION_DISCONNECT = "com.koksny.trikicontrol.DISCONNECT";

    private static final UUID NUS_SERVICE_UUID = UUID.fromString("6e400001-b5a3-f393-e0a9-e50e24dcca9e");
    private static final UUID UART_RX_UUID = UUID.fromString("6e400002-b5a3-f393-e0a9-e50e24dcca9e");
    private static final UUID UART_TX_UUID = UUID.fromString("6e400003-b5a3-f393-e0a9-e50e24dcca9e");
    private static final UUID LED_UUID = UUID.fromString("6e400004-b5a3-f393-e0a9-e50e24dcca9e");
    private static final UUID CLIENT_CONFIG_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    private static final byte[] START_STREAM = new byte[]{0x20, 0x10, 0x00, (byte) 0xd0, 0x07, 0x34, 0x00, 0x03};
    private static final String CHANNEL_ID = "triki_ble";
    private static final int NOTIFICATION_ID = 1102;
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static WeakReference<Listener> listener = new WeakReference<>(null);
    private static volatile TrikiControlService instance;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final MotionFrameParser parser = new MotionFrameParser();
    private final TrikiMotionEngine engine = new TrikiMotionEngine();
    private BluetoothLeScanner scanner;
    private BluetoothGatt gatt;
    private BluetoothGattCharacteristic uartRx;
    private BluetoothGattCharacteristic uartTx;
    private BluetoothGattCharacteristic led;
    private String phase = "idle";
    private String detail = "Gotowy";
    private String deviceName = "";
    private long samples;
    private long streamStartedMs;
    private long lastUiUpdateMs;
    private boolean calibrationComplete;
    private TrikiMotionEngine.State motion = new TrikiMotionEngine.State(
            TrikiMotionEngine.Action.IDLE,
            0.0,
            0.0,
            0.0,
            0.0,
            true
    );
    private TrikiMotionEngine.Action previousAction = TrikiMotionEngine.Action.IDLE;
    private TrikiMotionEngine.Action previousOutputAction = TrikiMotionEngine.Action.IDLE;
    private volatile ActionBindings bindings = ActionBindings.defaults();
    private boolean scanning;
    private volatile boolean outputEnabled;
    private volatile boolean outputWasActive;
    private boolean ledWriteInFlight;
    private Boolean pendingLed;

    static void registerListener(Listener value) {
        listener = new WeakReference<>(value);
        TrikiControlService service = instance;
        if (service != null) {
            service.publishState(true);
        } else {
            MAIN.post(() -> value.onTrikiState(idleState()));
        }
    }

    static void unregisterListener(Listener value) {
        Listener current = listener.get();
        if (current == value) {
            listener.clear();
        }
    }

    static void connect(Context context) {
        Intent intent = new Intent(context, TrikiControlService.class).setAction(ACTION_CONNECT);
        context.startForegroundService(intent);
    }

    static void disconnect(Context context) {
        TrikiControlService service = instance;
        if (service != null) {
            service.handler.post(service::disconnectAndStop);
            return;
        }
        context.stopService(new Intent(context, TrikiControlService.class));
    }

    static void reloadSettings() {
        TrikiControlService service = instance;
        if (service != null) {
            service.handler.post(service::applySettings);
        }
    }

    static void setLed(boolean enabled) {
        TrikiControlService service = instance;
        if (service != null) {
            service.handler.post(() -> service.writeLed(enabled));
        }
    }

    static void releaseOutput() {
        TrikiAccessibilityService.releaseAll();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        createNotificationChannel();
        applySettings();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_DISCONNECT.equals(action)) {
            disconnectAndStop();
            return START_NOT_STICKY;
        }
        startForeground(NOTIFICATION_ID, notification("Przygotowanie Bluetooth"));
        if (ACTION_CONNECT.equals(action)) {
            beginScan();
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        stopScan();
        closeGatt();
        TrikiAccessibilityService.releaseAll();
        instance = null;
        super.onDestroy();
    }

    private void beginScan() {
        if (!hasBluetoothPermissions()) {
            fail("Brak uprawnien Bluetooth");
            disconnectAndStopDelayed();
            return;
        }
        BluetoothManager manager = getSystemService(BluetoothManager.class);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        if (adapter == null) {
            fail("Ten telefon nie obsluguje Bluetooth");
            disconnectAndStopDelayed();
            return;
        }
        if (!adapter.isEnabled()) {
            fail("Wlacz Bluetooth i sproboj ponownie");
            disconnectAndStopDelayed();
            return;
        }

        stopScan();
        closeGatt();
        parser.reset();
        engine.reset();
        applySettings();
        samples = 0;
        calibrationComplete = false;
        previousAction = TrikiMotionEngine.Action.IDLE;
        scanner = adapter.getBluetoothLeScanner();
        if (scanner == null) {
            fail("Nie mozna uruchomic skanowania BLE");
            disconnectAndStopDelayed();
            return;
        }
        scanning = true;
        setPhase("scanning", "Szukam kapsla TRIKI. Nacisnij jego przycisk.");
        ScanSettings settings = new ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
                .build();
        scanner.startScan(null, settings, scanCallback);
        handler.postDelayed(scanTimeout, 15_000L);
    }

    private final Runnable scanTimeout = () -> {
        if (!scanning) {
            return;
        }
        stopScan();
        fail("Nie znaleziono kapsla. Nacisnij przycisk i sproboj ponownie.");
    };

    private final ScanCallback scanCallback = new ScanCallback() {
        @Override
        public void onScanResult(int callbackType, ScanResult result) {
            BluetoothDevice device = result.getDevice();
            String name = null;
            if (hasConnectPermission()) {
                name = device.getName();
            }
            if (name == null && result.getScanRecord() != null) {
                name = result.getScanRecord().getDeviceName();
            }
            boolean nameMatches = name != null && name.toLowerCase(Locale.ROOT).contains("triki");
            boolean serviceMatches = false;
            if (result.getScanRecord() != null && result.getScanRecord().getServiceUuids() != null) {
                for (ParcelUuid uuid : result.getScanRecord().getServiceUuids()) {
                    if (NUS_SERVICE_UUID.equals(uuid.getUuid())) {
                        serviceMatches = true;
                        break;
                    }
                }
            }
            if (!nameMatches && !serviceMatches) {
                return;
            }
            deviceName = name == null ? "TRIKI" : name;
            stopScan();
            connectGatt(device);
        }

        @Override
        public void onScanFailed(int errorCode) {
            scanning = false;
            fail("Skanowanie BLE nie powiodlo sie: " + errorCode);
        }
    };

    private void connectGatt(BluetoothDevice device) {
        if (!hasConnectPermission()) {
            fail("Brak uprawnienia do polaczenia Bluetooth");
            return;
        }
        setPhase("connecting", "Lacze z " + deviceName);
        gatt = device.connectGatt(this, false, gattCallback, BluetoothDevice.TRANSPORT_LE);
    }

    private final BluetoothGattCallback gattCallback = new BluetoothGattCallback() {
        @Override
        public void onConnectionStateChange(BluetoothGatt bluetoothGatt, int status, int newState) {
            if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                setPhase("connecting", "Odczytuje uslugi kapsla");
                if (hasConnectPermission()) {
                    bluetoothGatt.discoverServices();
                }
                return;
            }
            if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                ControlSettings.setOutputEnabled(TrikiControlService.this, false);
                TrikiAccessibilityService.releaseAll();
                fail("Kapsel zostal rozlaczony");
                closeGatt();
                return;
            }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail("Blad polaczenia GATT: " + status);
                closeGatt();
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt bluetoothGatt, int status) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                fail("Nie mozna odczytac uslug GATT: " + status);
                return;
            }
            BluetoothGattService service = bluetoothGatt.getService(NUS_SERVICE_UUID);
            if (service == null) {
                fail("Kapsel nie udostepnia uslugi TRIKI UART");
                return;
            }
            uartRx = service.getCharacteristic(UART_RX_UUID);
            uartTx = service.getCharacteristic(UART_TX_UUID);
            led = service.getCharacteristic(LED_UUID);
            if (uartRx == null || uartTx == null) {
                fail("Brakuje charakterystyk TRIKI UART");
                return;
            }
            if (!hasConnectPermission() || !bluetoothGatt.setCharacteristicNotification(uartTx, true)) {
                fail("Nie mozna wlaczyc powiadomien ruchu");
                return;
            }
            BluetoothGattDescriptor descriptor = uartTx.getDescriptor(CLIENT_CONFIG_UUID);
            if (descriptor == null || !writeDescriptor(bluetoothGatt, descriptor)) {
                fail("Nie mozna aktywowac strumienia ruchu");
            }
        }

        @Override
        public void onDescriptorWrite(BluetoothGatt bluetoothGatt, BluetoothGattDescriptor descriptor, int status) {
            if (!CLIENT_CONFIG_UUID.equals(descriptor.getUuid())) {
                return;
            }
            if (status != BluetoothGatt.GATT_SUCCESS || !writeCharacteristic(bluetoothGatt, uartRx, START_STREAM, false)) {
                fail("Kapsel odrzucil komende startu");
                return;
            }
            streamStartedMs = SystemClock.elapsedRealtime();
            calibrationComplete = false;
            setPhase("connected", "Polaczono. Trwa kalibracja pozycji spoczynkowej.");
        }

        @Override
        public void onCharacteristicChanged(
                BluetoothGatt bluetoothGatt,
                BluetoothGattCharacteristic characteristic,
                byte[] value
        ) {
            if (UART_TX_UUID.equals(characteristic.getUuid())) {
                consumeMotion(value);
            }
        }

        @SuppressWarnings("deprecation")
        @Override
        public void onCharacteristicChanged(BluetoothGatt bluetoothGatt, BluetoothGattCharacteristic characteristic) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
                    && UART_TX_UUID.equals(characteristic.getUuid())) {
                consumeMotion(characteristic.getValue());
            }
        }

        @Override
        public void onCharacteristicWrite(
                BluetoothGatt bluetoothGatt,
                BluetoothGattCharacteristic characteristic,
                int status
        ) {
            if (LED_UUID.equals(characteristic.getUuid())) {
                handler.post(TrikiControlService.this::finishLedWrite);
            }
        }
    };

    private void consumeMotion(byte[] payload) {
        if (payload == null) {
            return;
        }
        List<MotionSample> decoded = parser.feed(payload);
        for (MotionSample sample : decoded) {
            samples++;
            double elapsed = (SystemClock.elapsedRealtime() - streamStartedMs) / 1000.0;
            motion = engine.addSample(elapsed, sample);
            if (motion.valid) {
                if (!calibrationComplete && motion.action != TrikiMotionEngine.Action.SETTLING) {
                    calibrationComplete = true;
                    setPhase("connected", "Polaczono. Kapsel gotowy.");
                }
                routeMotion(motion);
            }
        }
        long now = SystemClock.elapsedRealtime();
        if (now - lastUiUpdateMs >= 100L) {
            lastUiUpdateMs = now;
            publishState(false);
        }
    }

    private void routeMotion(TrikiMotionEngine.State state) {
        TrikiMotionEngine.Action outputAction = bindings.map(state.action);
        if (!outputEnabled || !TrikiAccessibilityService.isRunning()) {
            if (outputWasActive) {
                TrikiAccessibilityService.releaseAll();
                outputWasActive = false;
            }
            previousAction = state.action;
            previousOutputAction = outputAction;
            return;
        }
        if (outputAction == TrikiMotionEngine.Action.STAMP || outputAction == TrikiMotionEngine.Action.SCRUB) {
            if (outputWasActive) {
                TrikiAccessibilityService.applyMotion(TrikiMotionEngine.Action.IDLE, 0.0);
                outputWasActive = false;
            }
            if (state.action != previousAction || outputAction != previousOutputAction) {
                TrikiAccessibilityService.tap(outputAction);
            }
        } else if (outputAction == TrikiMotionEngine.Action.IDLE
                || outputAction == TrikiMotionEngine.Action.SETTLING) {
            if (outputWasActive) {
                TrikiAccessibilityService.applyMotion(TrikiMotionEngine.Action.IDLE, 0.0);
                outputWasActive = false;
            }
        } else {
            TrikiAccessibilityService.applyMotion(outputAction, state.strength);
            outputWasActive = true;
        }
        previousAction = state.action;
        previousOutputAction = outputAction;
    }

    private void applySettings() {
        ControlSettings settings = ControlSettings.load(this);
        engine.configure(settings.turnThreshold, settings.turnSensitivity, settings.invertTurn);
        bindings = settings.bindings;
        outputEnabled = settings.outputEnabled;
        if (outputWasActive) {
            TrikiAccessibilityService.releaseAll();
            outputWasActive = false;
        }
        previousAction = TrikiMotionEngine.Action.IDLE;
        previousOutputAction = TrikiMotionEngine.Action.IDLE;
        publishState(true);
    }

    private void writeLed(boolean enabled) {
        if (ledWriteInFlight) {
            pendingLed = enabled;
            return;
        }
        BluetoothGatt currentGatt = gatt;
        BluetoothGattCharacteristic currentLed = led;
        if (currentGatt != null && currentLed != null) {
            ledWriteInFlight = writeCharacteristic(
                    currentGatt,
                    currentLed,
                    new byte[]{enabled ? (byte) 1 : (byte) 0},
                    true
            );
            if (!ledWriteInFlight && pendingLed != null) {
                handler.post(this::finishLedWrite);
            }
        }
    }

    private void finishLedWrite() {
        ledWriteInFlight = false;
        if (pendingLed == null) {
            return;
        }
        boolean next = pendingLed;
        pendingLed = null;
        writeLed(next);
    }

    @SuppressWarnings("deprecation")
    private boolean writeDescriptor(BluetoothGatt bluetoothGatt, BluetoothGattDescriptor descriptor) {
        if (!hasConnectPermission()) {
            return false;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return bluetoothGatt.writeDescriptor(
                    descriptor,
                    BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            ) == BluetoothStatusCodes.SUCCESS;
        }
        descriptor.setValue(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
        return bluetoothGatt.writeDescriptor(descriptor);
    }

    @SuppressWarnings("deprecation")
    private boolean writeCharacteristic(
            BluetoothGatt bluetoothGatt,
            BluetoothGattCharacteristic characteristic,
            byte[] value,
            boolean response
    ) {
        if (!hasConnectPermission()) {
            return false;
        }
        int writeType = response
                ? BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                : BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return bluetoothGatt.writeCharacteristic(characteristic, value, writeType)
                    == BluetoothStatusCodes.SUCCESS;
        }
        characteristic.setWriteType(writeType);
        characteristic.setValue(value);
        return bluetoothGatt.writeCharacteristic(characteristic);
    }

    private void stopScan() {
        handler.removeCallbacks(scanTimeout);
        if (scanning && scanner != null && hasScanPermission()) {
            scanner.stopScan(scanCallback);
        }
        scanning = false;
        scanner = null;
    }

    private void closeGatt() {
        BluetoothGatt current = gatt;
        gatt = null;
        uartRx = null;
        uartTx = null;
        led = null;
        ledWriteInFlight = false;
        pendingLed = null;
        if (current != null) {
            if (hasConnectPermission()) {
                current.disconnect();
                current.close();
            }
        }
    }

    private void disconnectAndStop() {
        stopScan();
        closeGatt();
        parser.reset();
        engine.reset();
        motion = new TrikiMotionEngine.State(
                TrikiMotionEngine.Action.IDLE,
                0.0,
                0.0,
                0.0,
                0.0,
                true
        );
        ControlSettings.setOutputEnabled(this, false);
        outputEnabled = false;
        outputWasActive = false;
        TrikiAccessibilityService.releaseAll();
        setPhase("idle", "Rozlaczono");
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    private void disconnectAndStopDelayed() {
        handler.postDelayed(this::disconnectAndStop, 1800L);
    }

    private void fail(String message) {
        setPhase("error", message);
    }

    private void setPhase(String nextPhase, String nextDetail) {
        phase = nextPhase;
        detail = nextDetail;
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, notification(nextDetail));
        }
        publishState(true);
    }

    private void publishState(boolean force) {
        Listener current = listener.get();
        if (current == null) {
            return;
        }
        JSONObject state = buildState();
        MAIN.post(() -> {
            Listener latest = listener.get();
            if (latest != null) {
                latest.onTrikiState(state);
            }
        });
    }

    private JSONObject buildState() {
        JSONObject json = new JSONObject();
        try {
            json.put("phase", phase);
            json.put("detail", detail);
            json.put("deviceName", deviceName);
            json.put("samples", samples);
            json.put("motion", motion.action.name().toLowerCase(Locale.ROOT));
            json.put("strength", motion.strength);
            json.put("twist", motion.twist);
            json.put("spin", motion.spin);
            json.put("tilt", motion.tilt);
            json.put("accessibilityRunning", TrikiAccessibilityService.isRunning());
            json.put("outputEnabled", outputEnabled);
        } catch (Exception ignored) {
            // Primitive state values are always serializable.
        }
        return json;
    }

    private static JSONObject idleState() {
        JSONObject json = new JSONObject();
        try {
            json.put("phase", "idle");
            json.put("detail", "Gotowy");
            json.put("deviceName", "");
            json.put("samples", 0);
            json.put("motion", "idle");
            json.put("strength", 0.0);
            json.put("twist", 0.0);
            json.put("spin", 0.0);
            json.put("tilt", 0.0);
            json.put("accessibilityRunning", TrikiAccessibilityService.isRunning());
            json.put("outputEnabled", false);
        } catch (Exception ignored) {
            // Primitive state values are always serializable.
        }
        return json;
    }

    private Notification notification(String text) {
        Intent launch = new Intent(this, MainActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                0,
                launch,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        Intent disconnect = new Intent(this, TrikiControlService.class).setAction(ACTION_DISCONNECT);
        PendingIntent disconnectIntent = PendingIntent.getService(
                this,
                1,
                disconnect,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(com.koksny.trikicontrol.R.drawable.ic_stat_triki)
                .setContentTitle(getString(com.koksny.trikicontrol.R.string.app_name))
                .setContentText(text)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .setCategory(Notification.CATEGORY_SERVICE)
                .addAction(com.koksny.trikicontrol.R.drawable.ic_stat_triki, "Rozlacz", disconnectIntent)
                .build();
    }

    private void createNotificationChannel() {
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                getString(com.koksny.trikicontrol.R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("Stan polaczenia Bluetooth z kapslem TRIKI");
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) {
            manager.createNotificationChannel(channel);
        }
    }

    private boolean hasBluetoothPermissions() {
        return hasScanPermission() && hasConnectPermission();
    }

    private boolean hasScanPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED;
        }
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasConnectPermission() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.S
                || checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
    }
}
