cmake_minimum_required(VERSION 3.22.1)
project(carrelay_bridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

# Google Oboe via Android Prefab (com.google.oboe:oboe in build.gradle.kts,
# android.buildFeatures.prefab = true). Provides the imported target oboe::oboe.
find_package(oboe REQUIRED CONFIG)

add_library(carrelay_bridge SHARED
        RingBuffer.h
        OboeAudioBridge.h
        OboeAudioBridge.cpp
        jni_bridge.cpp)

target_compile_options(carrelay_bridge PRIVATE
        -Wall -Wextra -Werror=return-type
        -O2 -ffast-math
        -fno-omit-frame-pointer)

target_link_libraries(carrelay_bridge
        PRIVATE
        oboe::oboe
        android
        log)
