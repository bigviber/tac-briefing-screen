import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// Release signing: build_and_package.ps1 writes keystore.properties at the repo
// root (storeFile / storePassword / keyAlias / keyPassword). Any self-signed key
// is fine for a /system/priv-app - privileges come from the location + the
// privapp-permissions whitelist, not from the platform key.
val keystoreProps = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "com.carivi.relay"
    compileSdk = 34
    ndkVersion = libs.versions.ndk.get()

    defaultConfig {
        applicationId = "com.carivi.relay"
        // SDK 30 contract: setConnectionPolicy/getConnectionPolicy, BLUETOOTH /
        // BLUETOOTH_ADMIN manifest permissions (no BLUETOOTH_CONNECT runtime flow).
        minSdk = 30
        targetSdk = 30
        versionCode = 3
        versionName = "1.0.2"

        ndk {
            abiFilters += listOf("arm64-v8a")
        }
        externalNativeBuild {
            cmake {
                arguments += listOf(
                    "-DANDROID_STL=c++_shared",
                    "-DANDROID_PLATFORM=android-30"
                )
                cppFlags += listOf("-std=c++17", "-fexceptions", "-frtti")
            }
        }
    }

    signingConfigs {
        create("release") {
            if (keystoreProps.isNotEmpty()) {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            isShrinkResources = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (keystoreProps.isNotEmpty()) signingConfig = signingConfigs.getByName("release")
        }
    }

    buildFeatures {
        compose = true
        // Google Oboe is consumed as an Android Prefab package (CMake find_package(oboe)).
        prefab = true
        buildConfig = true
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = libs.versions.cmake.get()
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        jniLibs {
            // Store .so files uncompressed + page aligned so PackageManager can
            // dlopen them from /system/priv-app/CarRelay/CarRelay.apk directly
            // (system apps are never extracted to /data/app-lib).
            useLegacyPackaging = false
        }
        resources {
            excludes += setOf("META-INF/*.kotlin_module", "META-INF/DEPENDENCIES")
        }
    }

    lint {
        abortOnError = false
    }
}

dependencies {
    implementation(project(":carrelay-core"))
    implementation(project(":solution3-audiopatch-bridge"))

    // Authorized libraries
    implementation(libs.oboe)
    implementation(libs.joor)
    implementation(libs.media3.session)
    implementation(libs.media3.common)
    implementation(libs.guava)
    implementation(libs.coroutines.android)
    implementation(libs.coroutines.guava)

    // Jetpack Compose Material3
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons.extended)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.service)
    implementation(libs.androidx.core.ktx)
}
