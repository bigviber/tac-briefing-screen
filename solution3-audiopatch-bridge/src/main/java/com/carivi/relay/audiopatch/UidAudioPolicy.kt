package com.carivi.relay.audiopatch

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.os.Looper
import android.util.Log
import com.carivi.relay.core.reflect.Hidden
import org.joor.Reflect

/**
 * Dynamic audio policies (android.media.audiopolicy.*, @SystemApi on SDK 30)
 * keyed on the Bluetooth process UID (1002 / android.uid.bluetooth).
 *
 * Why this works on the J606F: the A2DP sink decoder in libbluetooth_qti.so
 * plays the decoded iPhone PCM as an ordinary media AudioTrack owned by uid
 * 1002 (BtifAvrcpAudioTrack). A dynamic policy that matches RULE_MATCH_UID
 * therefore grabs exactly that stream and nothing else:
 *
 *  - [registerRenderMix]   ROUTE_FLAG_RENDER + setDevice(car A2DP output):
 *                          AudioFlinger renders the sink track straight to the
 *                          car output regardless of the default media device.
 *                          Zero copies, no JNI (Mode 3 alternative).
 *  - [registerLoopbackMix] ROUTE_FLAG_LOOP_BACK: the sink track is diverted to
 *                          a submix and handed back as an AudioRecord; the Oboe
 *                          engine pushes it to the car (Mode 2 fall-back when
 *                          the HAL "a2dp input" device yields no PCM).
 *
 * Both require MODIFY_AUDIO_ROUTING (priv-app whitelist).
 */
object UidAudioPolicy {
    private const val TAG = "CarRelay.UidPolicy"

    private const val CLS_RULE_BUILDER = "android.media.audiopolicy.AudioMixingRule\$Builder"
    private const val CLS_MIX_BUILDER = "android.media.audiopolicy.AudioMix\$Builder"
    private const val CLS_POLICY_BUILDER = "android.media.audiopolicy.AudioPolicy\$Builder"

    // AudioMixingRule
    const val RULE_MATCH_UID = 0x4
    // AudioMix
    const val ROUTE_FLAG_RENDER = 0x1
    const val ROUTE_FLAG_LOOP_BACK = 0x2
    // AudioManager.registerAudioPolicy result
    const val SUCCESS = 0

    const val MIX_SAMPLE_RATE = 48000

    /** Handle for a registered policy. */
    class Registration(
        val policy: Any,
        val mix: Any,
        val description: String
    )

    fun bluetoothUid(context: Context): Int = try {
        context.packageManager.getPackageUid("com.android.bluetooth", 0)
    } catch (t: Throwable) {
        1002
    }

    private fun mixFormat(): AudioFormat = AudioFormat.Builder()
        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
        .setSampleRate(MIX_SAMPLE_RATE)
        .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
        .build()

    private fun buildRule(uid: Int): Any? {
        val builder = Hidden.create(CLS_RULE_BUILDER).valueOrNull() ?: return null
        Hidden.call<Any>(builder, "addMixRule", RULE_MATCH_UID, Integer.valueOf(uid)).valueOrNull() ?: return null
        return Hidden.call<Any>(builder, "build").valueOrNull()
    }

    private fun buildMix(rule: Any, routeFlags: Int, device: AudioDeviceInfo?): Any? {
        val builder = Hidden.create(CLS_MIX_BUILDER, rule).valueOrNull() ?: return null
        Hidden.call<Any>(builder, "setFormat", mixFormat()).valueOrNull() ?: return null
        Hidden.call<Any>(builder, "setRouteFlags", routeFlags).valueOrNull() ?: return null
        if (device != null) {
            Hidden.call<Any>(builder, "setDevice", device).valueOrNull() ?: return null
        }
        return Hidden.call<Any>(builder, "build").valueOrNull()
    }

    private fun buildPolicy(context: Context, mix: Any): Any? {
        val builder = Hidden.create(CLS_POLICY_BUILDER, context).valueOrNull() ?: return null
        Hidden.call<Any>(builder, "addMix", mix).valueOrNull() ?: return null
        Hidden.call<Any>(builder, "setLooper", Looper.getMainLooper()).valueOrNull()
        return Hidden.call<Any>(builder, "build").valueOrNull()
    }

    private fun register(context: Context, policy: Any): Int {
        val am = context.getSystemService(AudioManager::class.java)
        return Hidden.call<Int>(am, "registerAudioPolicy", policy).valueOrNull() ?: -1
    }

    /** Route every player of [uid] directly to [device]. */
    fun registerRenderMix(context: Context, uid: Int, device: AudioDeviceInfo): Registration? {
        val rule = buildRule(uid) ?: return null.also { Log.w(TAG, "AudioMixingRule build failed") }
        val mix = buildMix(rule, ROUTE_FLAG_RENDER, device) ?: return null.also { Log.w(TAG, "AudioMix(render) build failed") }
        val policy = buildPolicy(context, mix) ?: return null.also { Log.w(TAG, "AudioPolicy build failed") }
        val status = register(context, policy)
        Log.i(TAG, "registerAudioPolicy(render uid=$uid -> ${device.productName}#${device.id}) = $status")
        if (status != SUCCESS) return null
        return Registration(policy, mix, "render uid $uid → ${device.productName} (#${device.id})")
    }

    /** Divert every player of [uid] into a loop-back submix and return the AudioRecord that reads it. */
    fun registerLoopbackMix(context: Context, uid: Int): Pair<Registration, AudioRecord>? {
        val rule = buildRule(uid) ?: return null.also { Log.w(TAG, "AudioMixingRule build failed") }
        val mix = buildMix(rule, ROUTE_FLAG_LOOP_BACK, null) ?: return null.also { Log.w(TAG, "AudioMix(loopback) build failed") }
        val policy = buildPolicy(context, mix) ?: return null.also { Log.w(TAG, "AudioPolicy build failed") }
        val status = register(context, policy)
        Log.i(TAG, "registerAudioPolicy(loopback uid=$uid) = $status")
        if (status != SUCCESS) return null
        val record = try {
            Reflect.on(policy).call("createAudioRecordSink", mix).get<AudioRecord>()
        } catch (t: Throwable) {
            Log.w(TAG, "createAudioRecordSink failed", t)
            unregister(context, Registration(policy, mix, "loopback"))
            return null
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "loopback AudioRecord not initialised (state=${record.state})")
            record.release()
            unregister(context, Registration(policy, mix, "loopback"))
            return null
        }
        return Registration(policy, mix, "loopback uid $uid @ ${MIX_SAMPLE_RATE}Hz stereo") to record
    }

    fun unregister(context: Context, registration: Registration) {
        val am = context.getSystemService(AudioManager::class.java)
        val r = Hidden.call<Unit>(am, "unregisterAudioPolicyAsync", registration.policy)
        Log.i(TAG, "unregisterAudioPolicyAsync(${registration.description}) -> ${r.describe()}")
    }
}
