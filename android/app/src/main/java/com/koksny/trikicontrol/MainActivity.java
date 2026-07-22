package com.koksny.trikicontrol;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Insets;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONObject;

public final class MainActivity extends Activity implements TrikiControlService.Listener {
    private static final int REQUEST_BLUETOOTH_PERMISSIONS = 4101;
    private static final int REQUEST_ENABLE_BLUETOOTH = 4102;

    private WebView webView;
    private boolean pageReady;
    private boolean pendingConnect;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        FrameLayout root = new FrameLayout(this);
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));
        setContentView(root);
        applySystemBarInsets(root);
        configureWebView();
        webView.loadUrl("file:///android_asset/index.html");
    }

    @SuppressWarnings("deprecation")
    private void applySystemBarInsets(View root) {
        root.setOnApplyWindowInsetsListener((view, windowInsets) -> {
            int left;
            int top;
            int right;
            int bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                Insets insets = windowInsets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                );
                left = insets.left;
                top = insets.top;
                right = insets.right;
                bottom = insets.bottom;
            } else {
                left = windowInsets.getSystemWindowInsetLeft();
                top = windowInsets.getSystemWindowInsetTop();
                right = windowInsets.getSystemWindowInsetRight();
                bottom = windowInsets.getSystemWindowInsetBottom();
            }
            view.setPadding(left, top, right, bottom);
            return windowInsets;
        });
        root.requestApplyInsets();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!TrikiAccessibilityService.isEnabled(this)) {
            ControlSettings.setOutputEnabled(this, false);
            TrikiControlService.releaseOutput();
        }
        TrikiControlService.registerListener(this);
        pushInitialState();
    }

    @Override
    protected void onPause() {
        TrikiControlService.unregisterListener(this);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.removeJavascriptInterface("Android");
            webView.destroy();
        }
        super.onDestroy();
    }

    @Override
    public void onTrikiState(JSONObject state) {
        evaluate("window.TRIKI_NATIVE_STATE && window.TRIKI_NATIVE_STATE(" + state + ");");
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_BLUETOOTH_PERMISSIONS) {
            return;
        }
        pushInitialState();
        if (pendingConnect && hasEssentialBluetoothPermissions()) {
            ensureBluetoothEnabledAndConnect();
        } else if (pendingConnect) {
            pendingConnect = false;
            sendError("Bluetooth wymaga zgody na wyszukiwanie i polaczenie z kapslem.");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_ENABLE_BLUETOOTH) {
            if (resultCode == RESULT_OK && pendingConnect) {
                pendingConnect = false;
                TrikiControlService.connect(this);
            } else {
                pendingConnect = false;
                sendError("Wlacz Bluetooth, aby polaczyc kapsel.");
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccess(true);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setMediaPlaybackRequiresUserGesture(true);
        webView.addJavascriptInterface(new NativeBridge(), "Android");
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                pageReady = true;
                pushInitialState();
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                return !"file".equalsIgnoreCase(uri.getScheme());
            }
        });
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true);
        }
    }

    private void requestConnect() {
        pendingConnect = true;
        if (!hasEssentialBluetoothPermissions()) {
            requestPermissions(requiredPermissions(), REQUEST_BLUETOOTH_PERMISSIONS);
            return;
        }
        ensureBluetoothEnabledAndConnect();
    }

    @SuppressLint("MissingPermission")
    private void ensureBluetoothEnabledAndConnect() {
        BluetoothManager manager = getSystemService(BluetoothManager.class);
        BluetoothAdapter adapter = manager == null ? null : manager.getAdapter();
        if (adapter == null) {
            pendingConnect = false;
            sendError("Ten telefon nie obsluguje Bluetooth Low Energy.");
            return;
        }
        if (!adapter.isEnabled()) {
            startActivityForResult(new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE), REQUEST_ENABLE_BLUETOOTH);
            return;
        }
        pendingConnect = false;
        TrikiControlService.connect(this);
    }

    private String[] requiredPermissions() {
        List<String> permissions = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            permissions.add(Manifest.permission.BLUETOOTH_SCAN);
            permissions.add(Manifest.permission.BLUETOOTH_CONNECT);
        } else {
            permissions.add(Manifest.permission.ACCESS_COARSE_LOCATION);
            permissions.add(Manifest.permission.ACCESS_FINE_LOCATION);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissions.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        return permissions.toArray(new String[0]);
    }

    private boolean hasEssentialBluetoothPermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            return checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED
                    && checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
        }
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    private void openAccessibilitySettings(boolean disclosureAccepted) {
        if (!disclosureAccepted) {
            sendError("Najpierw potwierdz informacje o sterowaniu dotykiem.");
            return;
        }
        ControlSettings.acceptDisclosure(this);
        Intent intent = new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
        startActivity(intent);
    }

    private void openInstalledApps() {
        Intent intent = new Intent(Settings.ACTION_MANAGE_APPLICATIONS_SETTINGS);
        try {
            startActivity(intent);
        } catch (ActivityNotFoundException ignored) {
            Intent fallback = new Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName())
            );
            startActivity(fallback);
        }
    }

    private void setOutputEnabled(boolean enabled) {
        if (enabled && !TrikiAccessibilityService.isEnabled(this)) {
            sendError("Wlacz usluge TRIKI Control w ustawieniach ulatwien dostepu.");
            pushInitialState();
            return;
        }
        ControlSettings.setOutputEnabled(this, enabled);
        TrikiControlService.reloadSettings();
        if (!enabled) {
            TrikiControlService.releaseOutput();
        }
        pushInitialState();
    }

    private void pushInitialState() {
        if (!pageReady || webView == null) {
            return;
        }
        JSONObject json = new JSONObject();
        try {
            json.put("settings", ControlSettings.load(this).toJson());
            json.put("accessibilityEnabled", TrikiAccessibilityService.isEnabled(this));
            json.put("accessibilityRunning", TrikiAccessibilityService.isRunning());
            json.put("accessibilityServiceName", getString(R.string.accessibility_service_name));
            json.put("bluetoothPermissions", hasEssentialBluetoothPermissions());
            json.put("sdkInt", Build.VERSION.SDK_INT);
            json.put("version", BuildConfig.VERSION_NAME);
            json.put("appLabel", getApplicationInfo().loadLabel(getPackageManager()).toString());
        } catch (Exception ignored) {
            // Primitive state values are always serializable.
        }
        evaluate("window.TRIKI_INITIAL_STATE && window.TRIKI_INITIAL_STATE(" + json + ");");
    }

    private void sendError(String message) {
        evaluate("window.TRIKI_ERROR && window.TRIKI_ERROR(" + JSONObject.quote(message) + ");");
    }

    private void evaluate(String script) {
        if (webView == null) {
            return;
        }
        runOnUiThread(() -> {
            if (pageReady && webView != null) {
                webView.evaluateJavascript(script, null);
            }
        });
    }

    private final class NativeBridge {
        @JavascriptInterface
        public void ready() {
            runOnUiThread(MainActivity.this::pushInitialState);
        }

        @JavascriptInterface
        public void connect() {
            runOnUiThread(MainActivity.this::requestConnect);
        }

        @JavascriptInterface
        public void disconnect() {
            runOnUiThread(() -> TrikiControlService.disconnect(MainActivity.this));
        }

        @JavascriptInterface
        public void openAccessibilitySettings(boolean disclosureAccepted) {
            runOnUiThread(() -> MainActivity.this.openAccessibilitySettings(disclosureAccepted));
        }

        @JavascriptInterface
        public void openInstalledApps() {
            runOnUiThread(MainActivity.this::openInstalledApps);
        }

        @JavascriptInterface
        public void setOutputEnabled(boolean enabled) {
            runOnUiThread(() -> MainActivity.this.setOutputEnabled(enabled));
        }

        @JavascriptInterface
        public void saveSettings(String value) {
            runOnUiThread(() -> {
                try {
                    ControlSettings.save(MainActivity.this, new JSONObject(value));
                    TrikiControlService.reloadSettings();
                    pushInitialState();
                } catch (Exception exception) {
                    sendError("Nie mozna zapisac ustawien: " + exception.getMessage());
                }
            });
        }

        @JavascriptInterface
        public void setLed(boolean enabled) {
            TrikiControlService.setLed(enabled);
        }

        @JavascriptInterface
        public void testGesture(String actionName, int delayMs, int durationMs) {
            runOnUiThread(() -> {
                if (!TrikiAccessibilityService.isEnabled(MainActivity.this)
                        || !TrikiAccessibilityService.isRunning()) {
                    sendError("Najpierw wlacz usluge sterowania dotykiem.");
                    return;
                }
                try {
                    TrikiMotionEngine.Action action = TrikiMotionEngine.Action.valueOf(actionName);
                    if (!TrikiAccessibilityService.runDelayedTest(action, delayMs, durationMs)) {
                        sendError("Usluga sterowania dotykiem nie jest aktywna.");
                    }
                } catch (IllegalArgumentException exception) {
                    sendError("Nieznany test gestu.");
                }
            });
        }

        @JavascriptInterface
        public void refresh() {
            runOnUiThread(MainActivity.this::pushInitialState);
        }
    }
}
