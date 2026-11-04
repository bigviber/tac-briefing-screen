package com.carivi.relay.core.reflect

import android.util.Log
import org.joor.Reflect
import org.joor.ReflectException

/**
 * Thin wrappers around jOOR so every hidden / @SystemApi call site is a single,
 * readable line and never throws into the engine code.
 *
 * The companion app is deployed to /system/priv-app with
 * android:usesNonSdkApi="true", so ApplicationInfo.isAllowedToUseHiddenApis()
 * is true and none of these calls hit the hidden-API denylist. service.sh also
 * relaxes the global hidden_api_policy for adb debugging sessions.
 */
object Hidden {
    private const val TAG = "CarRelay.Hidden"

    /** Result of a reflective call that distinguishes "absent on this build" from "threw". */
    sealed class Outcome<out T> {
        data class Ok<T>(val value: T) : Outcome<T>()
        data class Missing(val signature: String) : Outcome<Nothing>()
        data class Error(val signature: String, val cause: Throwable) : Outcome<Nothing>()

        val isOk: Boolean get() = this is Ok
        fun valueOrNull(): T? = (this as? Ok<T>)?.value
        fun describe(): String = when (this) {
            is Ok -> "ok(${value})"
            is Missing -> "missing($signature)"
            is Error -> "error($signature: ${cause.javaClass.simpleName}: ${cause.message})"
        }
    }

    /** Call an instance method by name, returning a typed [Outcome]. */
    fun <T> call(target: Any, method: String, vararg args: Any?): Outcome<T> {
        val sig = "${target.javaClass.name}.$method/${args.size}"
        return try {
            val r = Reflect.on(target).call(method, *args)
            @Suppress("UNCHECKED_CAST")
            Outcome.Ok(r.get<Any?>() as T)
        } catch (e: ReflectException) {
            if (isMissingMethod(e)) {
                Log.w(TAG, "method not on this build: $sig")
                Outcome.Missing(sig)
            } else {
                Log.w(TAG, "call failed: $sig", e)
                Outcome.Error(sig, unwrap(e))
            }
        } catch (t: Throwable) {
            Log.w(TAG, "call failed: $sig", t)
            Outcome.Error(sig, t)
        }
    }

    /** Call a static method on a class resolved by name. */
    fun <T> callStatic(className: String, method: String, vararg args: Any?): Outcome<T> {
        val sig = "$className.$method/${args.size}"
        return try {
            val r = Reflect.onClass(className).call(method, *args)
            @Suppress("UNCHECKED_CAST")
            Outcome.Ok(r.get<Any?>() as T)
        } catch (e: ReflectException) {
            if (isMissingMethod(e)) {
                Log.w(TAG, "static method not on this build: $sig")
                Outcome.Missing(sig)
            } else {
                Log.w(TAG, "static call failed: $sig", e)
                Outcome.Error(sig, unwrap(e))
            }
        } catch (t: Throwable) {
            Log.w(TAG, "static call failed: $sig", t)
            Outcome.Error(sig, t)
        }
    }

    /** Read a static field (e.g. a hidden constant), with a fallback default. */
    fun <T> staticField(className: String, field: String, default: T): T = try {
        Reflect.onClass(className).get<T>(field)
    } catch (t: Throwable) {
        Log.d(TAG, "static field $className.$field unavailable, using default $default")
        default
    }

    /** Read an instance field. */
    fun <T> field(target: Any, field: String): T? = try {
        Reflect.on(target).get<T>(field)
    } catch (t: Throwable) {
        null
    }

    /** Instantiate a (possibly hidden) class with constructor args. */
    fun create(className: String, vararg args: Any?): Outcome<Any> {
        val sig = "new $className/${args.size}"
        return try {
            Outcome.Ok(Reflect.onClass(className).create(*args).get())
        } catch (e: ReflectException) {
            Log.w(TAG, "ctor failed: $sig", e)
            if (isMissingMethod(e)) Outcome.Missing(sig) else Outcome.Error(sig, unwrap(e))
        } catch (t: Throwable) {
            Outcome.Error(sig, t)
        }
    }

    /** Resolve a hidden class or null. */
    fun classOrNull(className: String): Class<*>? = try {
        Class.forName(className)
    } catch (t: Throwable) {
        null
    }

    private fun isMissingMethod(e: ReflectException): Boolean {
        var c: Throwable? = e
        while (c != null) {
            if (c is NoSuchMethodException || c is NoSuchFieldException) return true
            c = c.cause
        }
        return false
    }

    private fun unwrap(e: ReflectException): Throwable {
        var c: Throwable = e
        while (c.cause != null && c.cause !== c) c = c.cause!!
        return c
    }
}
