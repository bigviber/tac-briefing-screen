pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // topjohnwu/libsu is published through JitPack.
        maven("https://jitpack.io")
    }
}

rootProject.name = "car-ivi-bt-relay"

// Shared contracts + hidden-API reflection helpers used by every architecture.
include(":carrelay-core")

// Solution 3: AudioFlinger-side bridges (AudioPatch + UID render policy). Library module.
include(":solution3-audiopatch-bridge")

// Solution 2: privileged companion app (Compose dashboard, role coordinator, Oboe engine).
include(":solution2-oboe-bridge-app:app")
