package com.koksny.trikicontrol;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.provider.Settings;
import android.webkit.WebView;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public final class AndroidUiContractTest {
    private static final long TIMEOUT_MS = 30_000L;
    private static final long POLL_MS = 25L;

    private Instrumentation instrumentation;
    private Context context;
    private MainActivity activity;
    private WebView webView;

    @Before
    public void setUp() throws Exception {
        instrumentation = InstrumentationRegistry.getInstrumentation();
        context = instrumentation.getTargetContext();
        preferences().edit().clear().commit();

        Intent intent = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        activity = (MainActivity) instrumentation.startActivitySync(intent);
        webView = findWebView(activity);
        waitForJavascript("document.readyState === 'complete'");
        waitForJavascript("typeof window.TRIKI_INITIAL_STATE === 'function'");
    }

    @After
    public void tearDown() {
        if (activity != null) {
            instrumentation.runOnMainSync(activity::finish);
        }
        preferences().edit().clear().commit();
    }

    @Test
    public void mappingDialogSavesThroughNativeBridge() throws Exception {
        assertEquals(
                "false",
                evaluate("document.querySelector('[data-mapping=\"turnLeftAction\"]').click();"
                        + "document.getElementById('mapping-modal').hidden")
        );

        assertEquals(
                "true",
                evaluate("document.querySelector('[data-output-action=\"SCROLL_DOWN\"]').click(); true")
        );
        waitForPreference("bind_turn_left", "SCROLL_DOWN");

        assertEquals("\"SCROLL_DOWN\"", evaluate("settings.turnLeftAction"));
        assertEquals(
                "true",
                evaluate("document.querySelector('[data-mapping=\"turnLeftAction\"] output')"
                        + ".textContent.indexOf('Przewi\\u0144 w d\\u00f3\\u0142') >= 0")
        );
    }

    @Test
    public void capFaceStaysNeutralWhileMotionChanges() throws Exception {
        evaluate("window.TRIKI_NATIVE_STATE({motion:'turn_right',strength:0.8}); true");
        assertEquals("true", evaluate("document.getElementById('cap-stage').classList.contains('motion-turn-right')"));
        assertEquals("true", evaluate("(function(){var s=getComputedStyle(document.querySelector('.mascot-mouth'));"
                + "return parseFloat(s.width) > parseFloat(s.height) * 3;})()"));

        evaluate("window.TRIKI_NATIVE_STATE({motion:'stamp',strength:1}); true");
        assertEquals("true", evaluate("document.getElementById('cap-stage').classList.contains('pulse-stamp')"));
        assertEquals("false", evaluate("document.getElementById('mascot-face').classList.contains('surprised')"));
    }

    @Test
    public void diagnosticControlsStayCollapsedUntilRequested() throws Exception {
        assertEquals("false", evaluate("document.getElementById('control-test').open"));
        assertEquals(
                "false",
                evaluate("document.querySelector('[data-test=TURN_LEFT]').getBoundingClientRect().height > 0")
        );

        evaluate("document.querySelector('#control-test summary').click(); true");
        assertEquals("true", evaluate("document.getElementById('control-test').open"));
        assertEquals(
                "true",
                evaluate("document.querySelector('[data-test=TURN_LEFT]').getBoundingClientRect().height > 0")
        );
    }

    @Test
    public void restrictedSettingsButtonRequestsInstalledAppsScreen() throws Exception {
        Instrumentation.ActivityMonitor monitor = instrumentation.addMonitor(
                new IntentFilter(Settings.ACTION_MANAGE_APPLICATIONS_SETTINGS),
                null,
                true
        );
        try {
            evaluate("document.getElementById('installed-apps-button').click(); true");
            for (int attempt = 0; monitor.getHits() == 0 && attempt < maxAttempts(); attempt++) {
                Thread.sleep(POLL_MS);
            }
            assertTrue("Expected ACTION_MANAGE_APPLICATIONS_SETTINGS", monitor.getHits() > 0);
        } finally {
            instrumentation.removeMonitor(monitor);
        }
    }

    private SharedPreferences preferences() {
        return context.getSharedPreferences(ControlSettings.PREFS, Context.MODE_PRIVATE);
    }

    private void waitForPreference(String key, String expected) throws Exception {
        for (int attempt = 0;
                !expected.equals(preferences().getString(key, null)) && attempt < maxAttempts();
                attempt++) {
            Thread.sleep(POLL_MS);
        }
        assertEquals(expected, preferences().getString(key, null));
    }

    private void waitForJavascript(String expression) throws Exception {
        for (int attempt = 0; attempt < maxAttempts(); attempt++) {
            if ("true".equals(evaluate(expression))) {
                return;
            }
            Thread.sleep(POLL_MS);
        }
        assertEquals("true", evaluate(expression));
    }

    private static int maxAttempts() {
        return (int) (TIMEOUT_MS / POLL_MS);
    }

    private String evaluate(String script) throws Exception {
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<>();
        instrumentation.runOnMainSync(() -> webView.evaluateJavascript(script, value -> {
            result.set(value);
            latch.countDown();
        }));
        assertTrue("JavaScript callback timed out", latch.await(TIMEOUT_MS, TimeUnit.MILLISECONDS));
        return result.get();
    }

    private static WebView findWebView(Activity activity) {
        android.view.View root = activity.getWindow().getDecorView();
        if (root instanceof WebView) {
            return (WebView) root;
        }
        return findWebView((android.view.ViewGroup) root);
    }

    private static WebView findWebView(android.view.ViewGroup parent) {
        for (int index = 0; index < parent.getChildCount(); index++) {
            android.view.View child = parent.getChildAt(index);
            if (child instanceof WebView) {
                return (WebView) child;
            }
            if (child instanceof android.view.ViewGroup) {
                WebView nested = findWebView((android.view.ViewGroup) child);
                if (nested != null) {
                    return nested;
                }
            }
        }
        throw new AssertionError("MainActivity does not contain a WebView");
    }
}
