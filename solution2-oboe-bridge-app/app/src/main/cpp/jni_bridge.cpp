// JNI glue for com.carivi.relay.engine.NativeBridgeLib (Kotlin `object` with
// @JvmStatic external functions -> static JNI signatures with jclass).
#include <jni.h>
#include <android/log.h>

#include <string>

#include "OboeAudioBridge.h"

#define LOG_TAG "CarRelay.JNI"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)

namespace {

carrelay::OboeAudioBridge& bridge() {
    static carrelay::OboeAudioBridge instance;
    return instance;
}

}  // namespace

extern "C" {

JNIEXPORT jint JNICALL
Java_com_carivi_relay_engine_NativeBridgeLib_nativeStartDuplex(JNIEnv* /*env*/, jclass /*clazz*/,
                                                                jint inputDeviceId,
                                                                jint outputDeviceId,
                                                                jint sampleRate,
                                                                jint channelCount) {
    carrelay::BridgeConfig cfg;
    cfg.inputDeviceId = inputDeviceId;
    cfg.outputDeviceId = outputDeviceId;
    cfg.sampleRate = sampleRate;
    cfg.channelCount = channelCount;
    cfg.pushMode = false;
    LOGI("startDuplex in=%d out=%d rate=%d ch=%d", inputDeviceId, outputDeviceId, sampleRate, channelCount);
    return static_cast<jint>(bridge().startBridge(cfg));
}

JNIEXPORT jint JNICALL
Java_com_carivi_relay_engine_NativeBridgeLib_nativeStartPush(JNIEnv* /*env*/, jclass /*clazz*/,
                                                              jint outputDeviceId,
                                                              jint sampleRate,
                                                              jint channelCount) {
    carrelay::BridgeConfig cfg;
    cfg.inputDeviceId = oboe::kUnspecified;
    cfg.outputDeviceId = outputDeviceId;
    cfg.sampleRate = sampleRate;
    cfg.channelCount = channelCount;
    cfg.pushMode = true;
    LOGI("startPush out=%d rate=%d ch=%d", outputDeviceId, sampleRate, channelCount);
    return static_cast<jint>(bridge().startBridge(cfg));
}

JNIEXPORT void JNICALL
Java_com_carivi_relay_engine_NativeBridgeLib_nativeStop(JNIEnv* /*env*/, jclass /*clazz*/) {
    bridge().stopBridge();
}

JNIEXPORT jint JNICALL
Java_com_carivi_relay_engine_NativeBridgeLib_nativeWritePcm(JNIEnv* env, jclass /*clazz*/,
                                                             jshortArray buffer, jint frames) {
    if (buffer == nullptr || frames <= 0) return 0;
    jshort* data = static_cast<jshort*>(env->GetPrimitiveArrayCritical(buffer, nullptr));
    if (data == nullptr) return 0;
    jint written = bridge().pushPcm(reinterpret_cast<const int16_t*>(data), frames);
    env->ReleasePrimitiveArrayCritical(buffer, data, JNI_ABORT);
    return written;
}

// Layout documented in NativeBridgeLib.kt (StatsIndex).
JNIEXPORT jlongArray JNICALL
Java_com_carivi_relay_engine_NativeBridgeLib_nativeGetStats(JNIEnv* env, jclass /*clazz*/) {
    carrelay::BridgeStats s = bridge().stats();
    jlong values[12] = {
        static_cast<jlong>(s.inputSampleRate),
        static_cast<jlong>(s.outputSampleRate),
        static_cast<jlong>(s.channelCount),
        static_cast<jlong>(s.inputXRuns),
        static_cast<jlong>(s.outputXRuns),
        static_cast<jlong>(s.restarts),
        static_cast<jlong>(s.ringFillFrames),
        static_cast<jlong>(s.slipCorrections),
        static_cast<jlong>(s.outputLatencyMs * 1000.0),  // micros
        static_cast<jlong>(s.underruns),
        static_cast<jlong>(s.running ? 1 : 0),
        static_cast<jlong>(s.pushMode ? 1 : 0),
    };
    jlongArray out = env->NewLongArray(12);
    if (out != nullptr) env->SetLongArrayRegion(out, 0, 12, values);
    return out;
}

JNIEXPORT jboolean JNICALL
Java_com_carivi_relay_engine_NativeBridgeLib_nativeIsRunning(JNIEnv* /*env*/, jclass /*clazz*/) {
    return bridge().isRunning() ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jstring JNICALL
Java_com_carivi_relay_engine_NativeBridgeLib_nativeLastError(JNIEnv* env, jclass /*clazz*/) {
    return env->NewStringUTF(bridge().lastError().c_str());
}

}  // extern "C"
