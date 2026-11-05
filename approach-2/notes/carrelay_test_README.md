# CarRelay test (dual Bluetooth solutions from tac-briefing-screen-main)

Source: `../tac-briefing-screen-main/` (left exactly as received). Build copy: `toolchain/build-carrelay/src`.
Built with the uncle's `build_and_package.ps1` + the project-local toolchain (Gradle 8.10.2, NDK 26.3.11579264, CMake 3.22.1 installed into toolchain/android-sdk 2026-11-04).

Changes in the build copy (nothing else differs):
1. `android:persistent="false"` (was true): a persistent system app that crashes at boot is the 2026-11-02 boot-loop class; service.sh still starts the relay service.
2. `media3-ui` dependency dropped: unused by the code; Windows Defender blocked its Gradle unpack.
3. Built with our Gradle, not the bundled gradle-wrapper.jar (unverified binary).
Build note: Defender also blocks Gradle transform renames ("Could not move temporary workspace"); `toolchain/build-carrelay/build_retry.ps1` finishes the rename and retries.

Checks on the built APK: privileged perms = BLUETOOTH_PRIVILEGED, MODIFY_AUDIO_ROUTING, CAPTURE_AUDIO_OUTPUT, WRITE_SECURE_SETTINGS (all in privapp-permissions-carrelay.xml; whitelist is ENFORCED on this tablet); merged extras are normal perms only; native libs stored + page aligned; signed with a self-made key (keys/ in the build copy).

Runs ON TOP of btdual_patch v5 (needed: it is what keeps both A2DP roles alive on the QTI stack).

Files: `carrelay_ivi-v1.0.0-test1.zip` (module), `snap.sh` (read-only state dump), `state_before.txt`.
