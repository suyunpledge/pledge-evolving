package com.forge.android.bridge
import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.SecureRandom
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
object BridgeToken {
    @Synchronized fun get(context: Context): String {
        val prefs = context.getSharedPreferences("bridge-private", Context.MODE_PRIVATE)
        return prefs.getString("token", null) ?: (UUID.randomUUID().toString() + UUID.randomUUID()).also {
            check(prefs.edit().putString("token", it).commit())
        }
    }

    @Synchronized fun credentialKey(context: Context): String {
        val alias = "forge.bridge.credential-vault"
        val prefs = context.getSharedPreferences("bridge-private", Context.MODE_PRIVATE)
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val wrapped = prefs.getString("wrapped-vault-key", null)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        val key = store.getKey(alias, null) as? SecretKey
        if (wrapped != null) {
            checkNotNull(key) { "Android credential vault key is unavailable" }
            val blob = Base64.decode(wrapped, Base64.NO_WRAP)
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, blob.copyOfRange(0, 12)))
            return Base64.encodeToString(cipher.doFinal(blob.copyOfRange(12, blob.size)),
                Base64.URL_SAFE or Base64.NO_WRAP)
        }
        val wrappingKey = key ?: KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,
            "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(alias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build())
            generateKey()
        }
        val secret = ByteArray(32).also { SecureRandom().nextBytes(it) }
        cipher.init(Cipher.ENCRYPT_MODE, wrappingKey)
        check(cipher.iv.size == 12)
        val blob = cipher.iv + cipher.doFinal(secret)
        check(prefs.edit().putString("wrapped-vault-key", Base64.encodeToString(blob, Base64.NO_WRAP)).commit())
        return Base64.encodeToString(secret, Base64.URL_SAFE or Base64.NO_WRAP)
    }
}
