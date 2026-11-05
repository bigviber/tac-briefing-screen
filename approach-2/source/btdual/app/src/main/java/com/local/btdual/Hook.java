package com.local.btdual;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

/**
 * Qualcomm's Bluetooth app decides in Config.addAudioProfiles(String) which audio profiles start: with
 * persist.vendor.service.bt.a2dp.sink=true it adds A2dpSinkService (receive from a phone) but drops A2dpService
 * (send to the car), and the reverse when false. This hook keeps A2dpService in the list, so with the sink property on
 * Android's normal Bluetooth startup brings up both roles. Nothing else in the Bluetooth app is touched.
 */
public class Hook implements IXposedHookLoadPackage {
    private static final String TAG = "BtDual: ";

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lp) {
        if (!"com.android.bluetooth".equals(lp.packageName)) return;
        try {
            XposedHelpers.findAndHookMethod("com.android.bluetooth.btservice.Config", lp.classLoader,
                "addAudioProfiles", String.class, new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam p) {
                        if ("A2dpService".equals(p.args[0]) && Boolean.FALSE.equals(p.getResult())) {
                            p.setResult(true);
                            XposedBridge.log(TAG + "kept A2dpService (source) alongside the sink");
                        }
                    }
                });
            XposedBridge.log(TAG + "hooked Config.addAudioProfiles in " + lp.processName);
        } catch (Throwable t) {
            XposedBridge.log(TAG + "hook failed: " + t);
        }
    }
}
