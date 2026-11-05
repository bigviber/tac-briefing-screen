# Minification is disabled for the priv-app build (see build.gradle.kts), these
# rules only matter if someone turns it on.
-keep class com.carivi.relay.** { *; }
-keep class org.joor.** { *; }
-keepclassmembers class com.carivi.relay.engine.NativeBridgeLib { native <methods>; }
-dontwarn org.joor.**
