package com.carivi.relay.bt

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.util.Log
import com.carivi.relay.core.BtConstants
import com.carivi.relay.core.reflect.Hidden
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Lazily-bound BluetoothProfile proxies for the SDK 30 profile ids we need:
 * A2DP (2, source side -> car), A2DP_SINK (11), AVRCP_CONTROLLER (12),
 * HEADSET_CLIENT (16) for the iPhone side and HEADSET (1, AG) for the car side.
 *
 * BluetoothAdapter.getProfileProxy() on Android 11 instantiates the hidden
 * proxy classes for these ids (BluetoothA2dpSink, BluetoothAvrcpController,
 * BluetoothHeadsetClient) even though they are not in the public SDK, so the
 * public entry point is enough; all method calls go through jOOR.
 */
class BtProfileProxies(private val context: Context) {
    private val tag = "CarRelay.Proxies"
    private val lock = Mutex()
    private val proxies = HashMap<Int, BluetoothProfile>()

    val adapter: BluetoothAdapter? =
        context.getSystemService(BluetoothManager::class.java)?.adapter

    fun bondedDevices(): List<BluetoothDevice> = try {
        adapter?.bondedDevices?.toList() ?: emptyList()
    } catch (t: Throwable) {
        emptyList()
    }

    fun device(address: String?): BluetoothDevice? = try {
        if (address.isNullOrBlank()) null else adapter?.getRemoteDevice(address.uppercase())
    } catch (t: Throwable) {
        null
    }

    /** Bind (or return the cached) proxy for [profileId]; null if unavailable within 5 s. */
    suspend fun get(profileId: Int): BluetoothProfile? = lock.withLock {
        proxies[profileId]?.let { return it }
        val adapter = adapter ?: return null
        val deferred = CompletableDeferred<BluetoothProfile?>()
        val ok = adapter.getProfileProxy(context, object : BluetoothProfile.ServiceListener {
            override fun onServiceConnected(profile: Int, proxy: BluetoothProfile) {
                deferred.complete(proxy)
            }
            override fun onServiceDisconnected(profile: Int) {
                proxies.remove(profile)
            }
        }, profileId)
        if (!ok) {
            Log.w(tag, "getProfileProxy($profileId) refused")
            return null
        }
        val proxy = withTimeoutOrNull(5_000) { deferred.await() }
        if (proxy == null) {
            Log.w(tag, "getProfileProxy($profileId) timed out")
        } else {
            proxies[profileId] = proxy
            Log.i(tag, "proxy bound for profile $profileId: ${proxy.javaClass.simpleName}")
        }
        proxy
    }

    suspend fun connectionState(profileId: Int, device: BluetoothDevice?): Int {
        if (device == null) return BtConstants.STATE_DISCONNECTED
        val proxy = get(profileId) ?: return BtConstants.STATE_DISCONNECTED
        return try {
            proxy.getConnectionState(device)
        } catch (t: Throwable) {
            Hidden.call<Int>(proxy, "getConnectionState", device).valueOrNull() ?: BtConstants.STATE_DISCONNECTED
        }
    }

    suspend fun connectedDevices(profileId: Int): List<BluetoothDevice> {
        val proxy = get(profileId) ?: return emptyList()
        return try {
            proxy.connectedDevices ?: emptyList()
        } catch (t: Throwable) {
            emptyList()
        }
    }

    /** Hidden `connect(BluetoothDevice)` on any profile proxy. */
    suspend fun connect(profileId: Int, device: BluetoothDevice): Boolean {
        val proxy = get(profileId) ?: return false
        return Hidden.call<Boolean>(proxy, "connect", device).valueOrNull() ?: false
    }

    /** Hidden `disconnect(BluetoothDevice)` on any profile proxy. */
    suspend fun disconnect(profileId: Int, device: BluetoothDevice): Boolean {
        val proxy = get(profileId) ?: return false
        return Hidden.call<Boolean>(proxy, "disconnect", device).valueOrNull() ?: false
    }

    /**
     * SDK 30 contract: setConnectionPolicy(BluetoothDevice, int) with
     * CONNECTION_POLICY_ALLOWED=100 / FORBIDDEN=0. Falls back to the legacy
     * setPriority() if a proxy on this build only carries the old name.
     * Returns a human-readable outcome for the dashboard log.
     */
    suspend fun setConnectionPolicy(profileId: Int, device: BluetoothDevice, policy: Int): String {
        val proxy = get(profileId) ?: return "profile $profileId: proxy unavailable"
        val name = proxy.javaClass.simpleName
        val r = Hidden.call<Boolean>(proxy, "setConnectionPolicy", device, policy)
        return when (r) {
            is Hidden.Outcome.Ok -> "$name.setConnectionPolicy(${device.address}, $policy) = ${r.value}"
            is Hidden.Outcome.Missing -> {
                val legacy = Hidden.call<Boolean>(
                    proxy, "setPriority", device,
                    if (policy == BtConstants.CONNECTION_POLICY_ALLOWED) BtConstants.PRIORITY_ON else BtConstants.PRIORITY_OFF
                )
                when (legacy) {
                    is Hidden.Outcome.Ok -> "$name.setPriority(${device.address}) = ${legacy.value} (setConnectionPolicy absent)"
                    else -> "$name: no connection policy API on SDK 30 (n/a - AVRCP CT follows the A2DP sink link)"
                }
            }
            is Hidden.Outcome.Error -> "$name.setConnectionPolicy failed: ${r.cause.message}"
        }
    }

    suspend fun getConnectionPolicy(profileId: Int, device: BluetoothDevice): Int {
        val proxy = get(profileId) ?: return BtConstants.CONNECTION_POLICY_UNKNOWN
        return Hidden.call<Int>(proxy, "getConnectionPolicy", device).valueOrNull()
            ?: Hidden.call<Int>(proxy, "getPriority", device).valueOrNull()
            ?: BtConstants.CONNECTION_POLICY_UNKNOWN
    }

    /** BluetoothA2dp.setActiveDevice(BluetoothDevice) - hidden on SDK 30. */
    suspend fun setActiveA2dpDevice(device: BluetoothDevice?): Boolean {
        val proxy = get(BtConstants.PROFILE_A2DP) ?: return false
        return Hidden.call<Boolean>(proxy, "setActiveDevice", device).valueOrNull() ?: false
    }

    suspend fun activeA2dpDevice(): BluetoothDevice? {
        val proxy = get(BtConstants.PROFILE_A2DP) ?: return null
        return Hidden.call<BluetoothDevice?>(proxy, "getActiveDevice").valueOrNull()
    }

    /** BluetoothA2dpSink.isA2dpPlaying(BluetoothDevice) */
    suspend fun isSinkPlaying(device: BluetoothDevice?): Boolean {
        if (device == null) return false
        val proxy = get(BtConstants.PROFILE_A2DP_SINK) ?: return false
        return Hidden.call<Boolean>(proxy, "isA2dpPlaying", device).valueOrNull() ?: false
    }

    fun close() {
        val adapter = adapter ?: return
        for ((id, proxy) in proxies) {
            try {
                adapter.closeProfileProxy(id, proxy)
            } catch (t: Throwable) {
                Log.w(tag, "closeProfileProxy($id) failed", t)
            }
        }
        proxies.clear()
    }
}
