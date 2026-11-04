// Google Oboe full-duplex PCM bridge: BT A2DP input device (iPhone sink link)
// -> BT A2DP output device (car source link). Solution Architecture 2.
#pragma once

#include <oboe/Oboe.h>
#include <oboe/FullDuplexStream.h>
#include <oboe/LatencyTuner.h>

#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include "RingBuffer.h"

namespace carrelay {

struct BridgeConfig {
    // AudioDeviceInfo ids from AudioManager.getDevices(); 0 = oboe::kUnspecified
    int32_t inputDeviceId = oboe::kUnspecified;
    int32_t outputDeviceId = oboe::kUnspecified;
    int32_t sampleRate = 48000;
    int32_t channelCount = 2;
    // false: FullDuplexStream (capture from inputDeviceId)
    // true : output only, PCM arrives through pushPcm() (AudioPolicy loop-back feed)
    bool pushMode = false;
};

struct BridgeStats {
    int32_t inputSampleRate = 0;
    int32_t outputSampleRate = 0;
    int32_t channelCount = 0;
    int32_t inputXRuns = 0;
    int32_t outputXRuns = 0;
    int32_t restarts = 0;
    int32_t ringFillFrames = 0;
    int64_t slipCorrections = 0;
    int64_t underruns = 0;
    double outputLatencyMs = 0.0;
    bool running = false;
    bool pushMode = false;
};

class OboeAudioBridge final : public oboe::FullDuplexStream,
                              public oboe::AudioStreamErrorCallback {
public:
    OboeAudioBridge();
    ~OboeAudioBridge() override;

    // Named startBridge/stopBridge on purpose: oboe::FullDuplexStream already
    // declares virtual Result start()/stop() for the stream pair.
    oboe::Result startBridge(const BridgeConfig& config);
    void stopBridge();

    // Push mode producer entry point (Java AudioRecord thread).
    int32_t pushPcm(const int16_t* interleaved, int32_t frames);

    BridgeStats stats();
    bool isRunning() const { return mRunning.load(std::memory_order_acquire); }
    std::string lastError();

    // --- oboe::FullDuplexStream -------------------------------------------
    oboe::DataCallbackResult onBothStreamsReady(const void* inputData,
                                                int numInputFrames,
                                                void* outputData,
                                                int numOutputFrames) override;

    // --- oboe::AudioStreamErrorCallback -----------------------------------
    void onErrorBeforeClose(oboe::AudioStream* stream, oboe::Result error) override;
    void onErrorAfterClose(oboe::AudioStream* stream, oboe::Result error) override;

private:
    // Output-only data callback used in push mode (FullDuplexStream's
    // onAudioReady must not run without an input stream).
    class PushCallback final : public oboe::AudioStreamDataCallback {
    public:
        explicit PushCallback(OboeAudioBridge& owner) : mOwner(owner) {}
        oboe::DataCallbackResult onAudioReady(oboe::AudioStream* stream,
                                              void* audioData,
                                              int32_t numFrames) override;
    private:
        OboeAudioBridge& mOwner;
    };

    oboe::Result openStreamsLocked();
    void closeStreamsLocked();
    void scheduleRestart(oboe::Result error);
    void renderFromRing(int16_t* out, int32_t numOutputFrames);
    void drainInputSurplus();
    void setError(const std::string& msg);

    std::recursive_mutex mLock;
    BridgeConfig mConfig;
    std::shared_ptr<oboe::AudioStream> mInput;
    std::shared_ptr<oboe::AudioStream> mOutput;
    std::unique_ptr<oboe::LatencyTuner> mTuner;
    PushCallback mPushCallback;

    SpscFrameRing<int16_t> mRing;
    std::vector<int16_t> mScratch;
    int32_t mBurstFrames = 192;
    int32_t mLowWaterFrames = 0;
    int32_t mHighWaterFrames = 0;

    std::atomic<bool> mRunning{false};
    std::atomic<bool> mRestartPending{false};
    std::atomic<int32_t> mRestarts{0};
    std::atomic<int64_t> mSlips{0};
    std::atomic<int64_t> mUnderruns{0};
    std::thread mRestartThread;
    std::string mLastError;
};

}  // namespace carrelay
