// Single-producer / single-consumer lock-free ring buffer of interleaved PCM
// frames. Used (a) inside the Oboe duplex callback to decouple the input read
// size from the output burst size and absorb clock drift, and (b) in push mode
// where the Java AudioRecord thread produces and the Oboe output callback
// consumes.
#pragma once

#include <atomic>
#include <cstdint>
#include <cstring>
#include <vector>

namespace carrelay {

template <typename T>
class SpscFrameRing {
public:
    SpscFrameRing() = default;

    // capacityFrames is rounded up to a power of two; channelCount >= 1.
    void reset(int32_t capacityFrames, int32_t channelCount) {
        int32_t cap = 1;
        while (cap < capacityFrames) cap <<= 1;
        mCapacityFrames = cap;
        mMask = cap - 1;
        mChannels = channelCount;
        mData.assign(static_cast<size_t>(cap) * channelCount, T{});
        mHead.store(0, std::memory_order_relaxed);
        mTail.store(0, std::memory_order_relaxed);
    }

    int32_t capacityFrames() const { return mCapacityFrames; }
    int32_t channels() const { return mChannels; }

    int32_t availableToRead() const {
        return static_cast<int32_t>(mHead.load(std::memory_order_acquire) -
                                    mTail.load(std::memory_order_acquire));
    }

    int32_t availableToWrite() const {
        return mCapacityFrames - availableToRead();
    }

    // Producer side. Returns frames actually written (drops the excess).
    int32_t write(const T* src, int32_t frames) {
        int32_t room = availableToWrite();
        if (frames > room) frames = room;
        uint64_t head = mHead.load(std::memory_order_relaxed);
        for (int32_t f = 0; f < frames; ++f) {
            size_t idx = static_cast<size_t>((head + f) & mMask) * mChannels;
            std::memcpy(&mData[idx], src + static_cast<size_t>(f) * mChannels, sizeof(T) * mChannels);
        }
        mHead.store(head + frames, std::memory_order_release);
        return frames;
    }

    // Consumer side. Returns frames actually read; the caller zero-fills the rest.
    int32_t read(T* dst, int32_t frames) {
        int32_t avail = availableToRead();
        if (frames > avail) frames = avail;
        uint64_t tail = mTail.load(std::memory_order_relaxed);
        for (int32_t f = 0; f < frames; ++f) {
            size_t idx = static_cast<size_t>((tail + f) & mMask) * mChannels;
            std::memcpy(dst + static_cast<size_t>(f) * mChannels, &mData[idx], sizeof(T) * mChannels);
        }
        mTail.store(tail + frames, std::memory_order_release);
        return frames;
    }

    // Consumer side: discard frames (drift correction - we are ahead).
    int32_t skip(int32_t frames) {
        int32_t avail = availableToRead();
        if (frames > avail) frames = avail;
        mTail.fetch_add(frames, std::memory_order_release);
        return frames;
    }

    void clear() {
        mTail.store(mHead.load(std::memory_order_acquire), std::memory_order_release);
    }

private:
    std::vector<T> mData;
    int32_t mCapacityFrames = 0;
    int32_t mMask = 0;
    int32_t mChannels = 1;
    std::atomic<uint64_t> mHead{0};
    std::atomic<uint64_t> mTail{0};
};

}  // namespace carrelay
