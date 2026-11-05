#include "OboeAudioBridge.h"

#include <android/log.h>

#include <algorithm>
#include <chrono>
#include <cstring>

#define LOG_TAG "CarRelay.Oboe"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace carrelay {

namespace {
// 500 ms of ring buffer: enough to ride out an A2DP sink jitter-buffer refill
// without ever blocking the real-time callback.
constexpr int32_t kRingMillis = 500;
// Ignore underruns in the first callbacks while FullDuplexStream drains/discards
// the start-up skew so the dashboard counters only show steady-state glitches.
constexpr int32_t kWarmupCallbacks = 24;
constexpr int32_t kLowWaterBursts = 2;
constexpr int32_t kHighWaterBursts = 6;
}  // namespace

OboeAudioBridge::OboeAudioBridge() : mPushCallback(*this) {}

OboeAudioBridge::~OboeAudioBridge() {
    stopBridge();
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
oboe::Result OboeAudioBridge::startBridge(const BridgeConfig& config) {
    // Make sure no restart worker is mid-flight before we reconfigure.
    mRunning.store(false, std::memory_order_release);
    if (mRestartThread.joinable()) mRestartThread.join();

    std::lock_guard<std::recursive_mutex> guard(mLock);
    closeStreamsLocked();

    mConfig = config;
    if (mConfig.channelCount < 1) mConfig.channelCount = 1;
    if (mConfig.channelCount > 2) mConfig.channelCount = 2;
    if (mConfig.sampleRate <= 0) mConfig.sampleRate = 48000;

    mRing.reset(mConfig.sampleRate * kRingMillis / 1000, mConfig.channelCount);
    mScratch.assign(static_cast<size_t>(mConfig.sampleRate / 10) * mConfig.channelCount, 0);
    mSlips.store(0);
    mUnderruns.store(0);
    mRestarts.store(0);
    mLastError.clear();

    oboe::Result result = openStreamsLocked();
    if (result != oboe::Result::OK) {
        setError(std::string("open failed: ") + oboe::convertToText(result));
        closeStreamsLocked();
        return result;
    }
    mRunning.store(true, std::memory_order_release);
    LOGI("bridge started (%s) in=%d out=%d rate=%d ch=%d burst=%d",
         mConfig.pushMode ? "push" : "duplex", mConfig.inputDeviceId, mConfig.outputDeviceId,
         mOutput ? mOutput->getSampleRate() : 0, mConfig.channelCount, mBurstFrames);
    return oboe::Result::OK;
}

void OboeAudioBridge::stopBridge() {
    mRunning.store(false, std::memory_order_release);
    if (mRestartThread.joinable()) mRestartThread.join();
    std::lock_guard<std::recursive_mutex> guard(mLock);
    closeStreamsLocked();
    mRing.clear();
    LOGI("bridge stopped");
}

oboe::Result OboeAudioBridge::openStreamsLocked() {
    // ---- Output (car A2DP source link) ------------------------------------
    // Opened first: its negotiated sample rate / burst size is what the input
    // side has to follow. SharingMode::Shared keeps the mixer in the path so
    // local navigation prompts (Google Maps) are mixed into the same car link.
    oboe::AudioStreamBuilder outBuilder;
    outBuilder.setDirection(oboe::Direction::Output)
        ->setPerformanceMode(oboe::PerformanceMode::LowLatency)
        ->setSharingMode(oboe::SharingMode::Shared)
        ->setFormat(oboe::AudioFormat::I16)
        ->setFormatConversionAllowed(true)
        ->setChannelCount(mConfig.channelCount)
        ->setChannelConversionAllowed(true)
        ->setSampleRate(mConfig.sampleRate)
        ->setSampleRateConversionQuality(oboe::SampleRateConversionQuality::Medium)
        ->setUsage(oboe::Usage::Media)
        ->setContentType(oboe::ContentType::Music)
        ->setDeviceId(mConfig.outputDeviceId)
        ->setErrorCallback(this);
    if (mConfig.pushMode) {
        outBuilder.setDataCallback(&mPushCallback);
    } else {
        outBuilder.setDataCallback(this);
    }

    oboe::Result result = outBuilder.openStream(mOutput);
    if (result != oboe::Result::OK) {
        LOGE("output open failed: %s", oboe::convertToText(result));
        mOutput.reset();
        return result;
    }

    mBurstFrames = std::max(32, mOutput->getFramesPerBurst());
    mOutput->setBufferSizeInFrames(mBurstFrames * 3);
    mLowWaterFrames = mBurstFrames * kLowWaterBursts;
    mHighWaterFrames = mBurstFrames * kHighWaterBursts;
    mTuner = std::make_unique<oboe::LatencyTuner>(*mOutput);

    LOGI("output: id=%d rate=%d ch=%d burst=%d buf=%d perf=%d sharing=%d api=%s",
         mOutput->getDeviceId(), mOutput->getSampleRate(), mOutput->getChannelCount(),
         mOutput->getFramesPerBurst(), mOutput->getBufferSizeInFrames(),
         static_cast<int>(mOutput->getPerformanceMode()), static_cast<int>(mOutput->getSharingMode()),
         oboe::convertToText(mOutput->getAudioApi()));

    if (mConfig.pushMode) {
        result = mOutput->requestStart();
        if (result != oboe::Result::OK) {
            LOGE("output start failed: %s", oboe::convertToText(result));
        }
        return result;
    }

    // ---- Input (iPhone A2DP sink link, AUDIO_DEVICE_IN_BLUETOOTH_A2DP) -------
    oboe::AudioStreamBuilder inBuilder;
    inBuilder.setDirection(oboe::Direction::Input)
        ->setPerformanceMode(oboe::PerformanceMode::LowLatency)
        ->setSharingMode(oboe::SharingMode::Shared)
        ->setFormat(oboe::AudioFormat::I16)
        ->setFormatConversionAllowed(true)
        ->setChannelCount(mConfig.channelCount)
        ->setChannelConversionAllowed(true)
        ->setSampleRate(mOutput->getSampleRate())
        ->setSampleRateConversionQuality(oboe::SampleRateConversionQuality::Medium)
        ->setInputPreset(oboe::InputPreset::Unprocessed)
        ->setDeviceId(mConfig.inputDeviceId)
        ->setErrorCallback(this);

    result = inBuilder.openStream(mInput);
    if (result != oboe::Result::OK) {
        LOGE("input open failed: %s", oboe::convertToText(result));
        mInput.reset();
        return result;
    }
    // A generous input buffer: the sink side delivers in A2DP-packet sized
    // bursts, we drain the surplus ourselves in drainInputSurplus().
    mInput->setBufferSizeInFrames(mInput->getBufferCapacityInFrames());

    LOGI("input: id=%d rate=%d ch=%d burst=%d cap=%d api=%s",
         mInput->getDeviceId(), mInput->getSampleRate(), mInput->getChannelCount(),
         mInput->getFramesPerBurst(), mInput->getBufferCapacityInFrames(),
         oboe::convertToText(mInput->getAudioApi()));

    setSharedInputStream(mInput);
    setSharedOutputStream(mOutput);
    setNumInputBurstsCushion(2);

    result = FullDuplexStream::start();
    if (result != oboe::Result::OK) {
        LOGE("duplex start failed: %s", oboe::convertToText(result));
    }
    return result;
}

void OboeAudioBridge::closeStreamsLocked() {
    if (mInput && mOutput && !mConfig.pushMode) {
        FullDuplexStream::stop();
    } else if (mOutput) {
        mOutput->requestStop();
    }
    if (mInput) {
        mInput->close();
        mInput.reset();
    }
    if (mOutput) {
        mOutput->close();
        mOutput.reset();
    }
    std::shared_ptr<oboe::AudioStream> none;
    setSharedInputStream(none);
    setSharedOutputStream(none);
    mTuner.reset();
}

// ---------------------------------------------------------------------------
// Real-time path
// ---------------------------------------------------------------------------
oboe::DataCallbackResult OboeAudioBridge::onBothStreamsReady(const void* inputData,
                                                             int numInputFrames,
                                                             void* outputData,
                                                             int numOutputFrames) {
    if (numInputFrames > 0 && inputData != nullptr) {
        mRing.write(static_cast<const int16_t*>(inputData), numInputFrames);
    }
    drainInputSurplus();
    renderFromRing(static_cast<int16_t*>(outputData), numOutputFrames);
    if (mTuner) mTuner->tune();
    return oboe::DataCallbackResult::Continue;
}

oboe::DataCallbackResult OboeAudioBridge::PushCallback::onAudioReady(oboe::AudioStream* /*stream*/,
                                                                     void* audioData,
                                                                     int32_t numFrames) {
    mOwner.renderFromRing(static_cast<int16_t*>(audioData), numFrames);
    if (mOwner.mTuner) mOwner.mTuner->tune();
    return oboe::DataCallbackResult::Continue;
}

// FullDuplexStream only reads numOutputFrames per callback. If the input clock
// runs slightly fast the surplus would pile up in the AAudio input buffer and
// end in an input overrun. Pull it into our own ring instead; the slip logic in
// renderFromRing() then trims it one frame at a time.
void OboeAudioBridge::drainInputSurplus() {
    oboe::AudioStream* in = getInputStream();
    if (in == nullptr) return;
    oboe::ResultWithValue<int32_t> avail = in->getAvailableFrames();
    if (!avail) return;
    int32_t pending = avail.value();
    const int32_t scratchFrames = static_cast<int32_t>(mScratch.size()) / mConfig.channelCount;
    int guard = 8;
    while (pending > mBurstFrames && mRing.availableToWrite() > 0 && guard-- > 0) {
        int32_t chunk = std::min({pending, mBurstFrames, scratchFrames, mRing.availableToWrite()});
        oboe::ResultWithValue<int32_t> rr = in->read(mScratch.data(), chunk, 0);
        if (!rr || rr.value() <= 0) break;
        mRing.write(mScratch.data(), rr.value());
        pending -= rr.value();
    }
}

void OboeAudioBridge::renderFromRing(int16_t* out, int32_t numOutputFrames) {
    static thread_local int32_t warmup = 0;
    const int32_t ch = mConfig.channelCount;
    const size_t frameBytes = sizeof(int16_t) * ch;
    int32_t fill = mRing.availableToRead();

    // Drift compensation (hysteresis band between low/high water marks):
    //  * ring too full  -> consumer is behind the producer clock: drop one frame
    //  * ring too empty -> consumer is ahead: duplicate the last frame
    // One frame per burst keeps the correction inaudible (~0.02% rate error).
    if (fill > mHighWaterFrames) {
        mRing.skip(1);
        mSlips.fetch_add(1, std::memory_order_relaxed);
        fill--;
    }

    if (fill == 0) {
        std::memset(out, 0, frameBytes * numOutputFrames);
        // Push mode: an empty ring simply means the iPhone is paused.
        if (!mConfig.pushMode && warmup >= kWarmupCallbacks) {
            mUnderruns.fetch_add(1, std::memory_order_relaxed);
        }
        if (warmup < kWarmupCallbacks) warmup++;
        return;
    }

    if (fill < mLowWaterFrames && fill >= numOutputFrames && numOutputFrames > 1) {
        int32_t got = mRing.read(out, numOutputFrames - 1);
        std::memcpy(out + static_cast<size_t>(got) * ch,
                    out + static_cast<size_t>(got - 1) * ch, frameBytes);
        mSlips.fetch_add(1, std::memory_order_relaxed);
        if (warmup < kWarmupCallbacks) warmup++;
        return;
    }

    int32_t got = mRing.read(out, numOutputFrames);
    if (got < numOutputFrames) {
        std::memset(out + static_cast<size_t>(got) * ch, 0,
                    frameBytes * static_cast<size_t>(numOutputFrames - got));
        if (warmup >= kWarmupCallbacks) {
            mUnderruns.fetch_add(1, std::memory_order_relaxed);
        }
    }
    if (warmup < kWarmupCallbacks) warmup++;
}

int32_t OboeAudioBridge::pushPcm(const int16_t* interleaved, int32_t frames) {
    if (!mRunning.load(std::memory_order_acquire) || !mConfig.pushMode) return 0;
    return mRing.write(interleaved, frames);
}

// ---------------------------------------------------------------------------
// Error handling / automatic recovery
// ---------------------------------------------------------------------------
void OboeAudioBridge::onErrorBeforeClose(oboe::AudioStream* stream, oboe::Result error) {
    LOGW("stream %s error before close: %s",
         stream->getDirection() == oboe::Direction::Input ? "input" : "output",
         oboe::convertToText(error));
}

void OboeAudioBridge::onErrorAfterClose(oboe::AudioStream* stream, oboe::Result error) {
    const char* dir = stream->getDirection() == oboe::Direction::Input ? "input" : "output";
    LOGW("stream %s closed after error: %s", dir, oboe::convertToText(error));
    setError(std::string(dir) + ": " + oboe::convertToText(error));
    if (!mRunning.load(std::memory_order_acquire)) return;
    // ErrorDisconnected = the Bluetooth device went away / routing changed.
    // Reopen both streams; AAudio re-resolves the device ids on open.
    scheduleRestart(error);
}

void OboeAudioBridge::scheduleRestart(oboe::Result error) {
    if (mRestartPending.exchange(true)) return;
    if (mRestartThread.joinable()) mRestartThread.join();
    mRestartThread = std::thread([this, error]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(350));
        std::lock_guard<std::recursive_mutex> guard(mLock);
        if (!mRunning.load(std::memory_order_acquire)) {
            mRestartPending.store(false);
            return;
        }
        LOGI("restarting streams after %s", oboe::convertToText(error));
        closeStreamsLocked();
        mRing.clear();
        oboe::Result r = openStreamsLocked();
        if (r == oboe::Result::OK) {
            mRestarts.fetch_add(1);
            LOGI("restart ok (#%d)", mRestarts.load());
        } else {
            setError(std::string("restart failed: ") + oboe::convertToText(r));
            closeStreamsLocked();
            mRunning.store(false, std::memory_order_release);
            LOGE("restart failed: %s", oboe::convertToText(r));
        }
        mRestartPending.store(false);
    });
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------
BridgeStats OboeAudioBridge::stats() {
    BridgeStats s;
    std::lock_guard<std::recursive_mutex> guard(mLock);
    s.running = mRunning.load(std::memory_order_acquire);
    s.pushMode = mConfig.pushMode;
    s.channelCount = mConfig.channelCount;
    if (mOutput) {
        s.outputSampleRate = mOutput->getSampleRate();
        s.channelCount = mOutput->getChannelCount();
        if (auto x = mOutput->getXRunCount()) s.outputXRuns = x.value();
        if (auto lat = mOutput->calculateLatencyMillis()) s.outputLatencyMs = lat.value();
    }
    if (mInput) {
        s.inputSampleRate = mInput->getSampleRate();
        if (auto x = mInput->getXRunCount()) s.inputXRuns = x.value();
    } else if (mConfig.pushMode) {
        s.inputSampleRate = mConfig.sampleRate;
    }
    s.restarts = mRestarts.load();
    s.ringFillFrames = mRing.availableToRead();
    s.slipCorrections = mSlips.load();
    s.underruns = mUnderruns.load();
    return s;
}

std::string OboeAudioBridge::lastError() {
    std::lock_guard<std::recursive_mutex> guard(mLock);
    return mLastError;
}

void OboeAudioBridge::setError(const std::string& msg) {
    std::lock_guard<std::recursive_mutex> guard(mLock);
    mLastError = msg;
}

}  // namespace carrelay
