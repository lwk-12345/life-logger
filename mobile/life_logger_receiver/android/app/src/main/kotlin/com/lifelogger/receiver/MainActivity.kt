package com.lifelogger.receiver

import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val channelName = "life_logger_receiver/platform"
    private val localNetworkPermission = "android.permission.ACCESS_LOCAL_NETWORK"
    private val permissionRequestCode = 7412
    private var pendingPermissionResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "appSupportDirectory" -> {
                        val directory = getDir("life_logger_receiver", MODE_PRIVATE)
                        result.success(directory.absolutePath)
                    }
                    "requestLocalNetworkPermission" -> requestLocalNetworkAccess(result)
                    "startForegroundReceiver" -> {
                        val intent = Intent(this, ReceiverForegroundService::class.java)
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(intent)
                        } else {
                            startService(intent)
                        }
                        result.success(true)
                    }
                    "stopForegroundReceiver" -> {
                        stopService(Intent(this, ReceiverForegroundService::class.java))
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            }
    }

    private fun requestLocalNetworkAccess(result: MethodChannel.Result) {
        if (Build.VERSION.SDK_INT < 37 ||
            checkSelfPermission(localNetworkPermission) == PackageManager.PERMISSION_GRANTED) {
            result.success(true)
            return
        }
        if (pendingPermissionResult != null) {
            result.error("permission_pending", "Local network permission request is already active", null)
            return
        }
        pendingPermissionResult = result
        requestPermissions(arrayOf(localNetworkPermission), permissionRequestCode)
    }

    @Deprecated("Permission callback used for Android local-network permission")
    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != permissionRequestCode) return
        val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
        pendingPermissionResult?.success(granted)
        pendingPermissionResult = null
    }
}
