package com.samecast.receiver

import android.util.Log
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.SocketTimeoutException
import java.net.URL
import kotlin.concurrent.thread

/**
 * 与网关通信：先用 SSDP 自动找到网关，然后轮询拿指令、上报播放状态。
 *
 * 协议与网页接收端(/tv)完全一致：
 *   GET  <base>/api/recv/<id>/poll?since=<seq>
 *   POST <base>/api/recv/<id>/state    {"state":"playing","position":12.3}
 *   POST <base>/api/recv/<id>/command  {"cmd":"pause"}
 */
class GatewayClient(
    private val receiverId: String,
    private val deviceName: String,
    private val onCommand: (JSONObject) -> Unit,
    private val onStatus: (String) -> Unit,
) {
    companion object {
        private const val TAG = "samecast"
        private const val DISCOVERY_ST = "urn:samecast:service:Gateway:1"
    }

    @Volatile private var base: String? = null
    @Volatile private var running = true
    @Volatile private var seq = 0L

    fun start() {
        thread(name = "gateway-loop", isDaemon = true) { loop() }
    }

    fun stop() {
        running = false
    }

    private fun loop() {
        while (running) {
            try {
                var current = base
                if (current == null) {
                    onStatus("正在查找网关…")
                    current = discover()
                    if (current == null) {
                        onStatus("没找到网关，请确认网关和电视在同一局域网")
                        Thread.sleep(2000)
                        continue
                    }
                    base = current
                    seq = 0
                    onStatus("已连接网关")
                }
                poll(current)
            } catch (e: Exception) {
                Log.w(TAG, "连接异常: " + e.message)
                base = null
                Thread.sleep(1500)
            }
        }
    }

    /** SSDP 搜索网关，电视端不需要输入任何 IP。 */
    private fun discover(): String? {
        val msg = ("M-SEARCH * HTTP/1.1\r\n" +
                "HOST: 239.255.255.250:1900\r\n" +
                "MAN: \"ssdp:discover\"\r\n" +
                "MX: 2\r\n" +
                "ST: " + DISCOVERY_ST + "\r\n\r\n")
        DatagramSocket().use { socket ->
            socket.soTimeout = 1500
            socket.broadcast = true
            val data = msg.toByteArray()
            val target = InetAddress.getByName("239.255.255.250")
            repeat(2) {
                socket.send(DatagramPacket(data, data.size, target, 1900))
            }
            val buf = ByteArray(2048)
            val deadline = System.currentTimeMillis() + 4000
            while (System.currentTimeMillis() < deadline) {
                val packet = DatagramPacket(buf, buf.size)
                try {
                    socket.receive(packet)
                } catch (e: SocketTimeoutException) {
                    continue
                }
                val text = String(packet.data, 0, packet.length)
                val location = text.lineSequence()
                    .firstOrNull { it.startsWith("LOCATION:", ignoreCase = true) }
                    ?.substringAfter(":")
                    ?.trim()
                    ?: continue
                Log.i(TAG, "发现网关: " + location)
                // LOCATION 指向设备描述 XML（/device.xml 或 /bilibili/description.xml），API 根路径就是去掉这一段
                return apiBase(location)
            }
        }
        return null
    }

    /** 从设备描述 URL 里取出网关 API 根地址，不依赖具体描述文件路径。 */
    private fun apiBase(location: String): String {
        for (suffix in arrayOf("/device.xml", "/description.xml")) {
            if (location.endsWith(suffix)) return location.substring(0, location.length - suffix.length)
        }
        return location.substringBeforeLast('/')
    }

    private fun poll(base: String) {
        val url = URL(base + "/api/recv/" + receiverId + "/poll?since=" + seq)
        val conn = url.openConnection() as HttpURLConnection
        conn.connectTimeout = 3000
        conn.readTimeout = 6000
        val text = conn.inputStream.bufferedReader().use { it.readText() }
        conn.disconnect()

        val obj = JSONObject(text)
        seq = obj.optLong("seq", seq)
        val commands = obj.optJSONArray("commands")
        if (commands != null) {
            for (i in 0 until commands.length()) {
                onCommand(commands.getJSONObject(i))
            }
        }
        Thread.sleep(350)
    }

    fun report(state: String, position: Double, duration: Double = 0.0, preempted: Boolean = false) {
        val payload = JSONObject().put("state", state).put("position", position)
            .put("name", deviceName)
        if (duration > 0) payload.put("duration", duration)
        // 只有被抢占时上报该字段；恢复正常后不再带上，网关据此取消让位状态
        if (preempted) payload.put("preempted", true)
        post("/api/recv/" + receiverId + "/state", payload)
    }

    fun sendCommand(cmd: JSONObject) {
        post("/api/recv/" + receiverId + "/command", cmd)
    }

    /** 网关是否可达（SSDP 已找到并保持轮询）。 */
    fun isOnline(): Boolean = base != null

    /**
     * 读取整组状态（GET /api/status）：同步面板拿它算每台电视与整组的时间偏差。
     * 回调在主线程之外执行，调用方自己 post 回主线程。
     */
    fun fetchStatus(callback: (JSONObject?) -> Unit) {
        val current = base
        if (current == null) {
            callback(null)
            return
        }
        thread(name = "gateway-status", isDaemon = true) {
            val result = try {
                val conn = URL(current + "/api/status").openConnection() as HttpURLConnection
                conn.connectTimeout = 2000
                conn.readTimeout = 2000
                val text = conn.inputStream.bufferedReader().use { it.readText() }
                conn.disconnect()
                JSONObject(text)
            } catch (e: Exception) {
                Log.d(TAG, "读取整组状态失败: " + e.message)
                null
            }
            callback(result)
        }
    }

    /** 整组控制（POST /api/control），{"cmd":"resync"} = 把所有电视拉回同一时刻。 */
    fun control(cmd: String, callback: (JSONObject?, Boolean) -> Unit) {
        val current = base
        if (current == null) {
            callback(null, false)
            return
        }
        thread(name = "gateway-control", isDaemon = true) {
            var body: JSONObject? = null
            var ok = false
            try {
                val conn = URL(current + "/api/control").openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.connectTimeout = 2000
                conn.readTimeout = 3000
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use {
                    it.write(JSONObject().put("cmd", cmd).toString())
                }
                val text = conn.inputStream.bufferedReader().use { it.readText() }
                conn.disconnect()
                body = try {
                    JSONObject(text)
                } catch (e: Exception) {
                    null
                }
                ok = true
            } catch (e: Exception) {
                Log.w(TAG, "整组控制失败: " + e.message)
            }
            callback(body, ok)
        }
    }

    private fun post(path: String, payload: JSONObject) {
        val current = base ?: return
        thread(isDaemon = true) {
            try {
                val conn = URL(current + path).openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.connectTimeout = 3000
                conn.readTimeout = 3000
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                OutputStreamWriter(conn.outputStream, Charsets.UTF_8).use { it.write(payload.toString()) }
                conn.inputStream.use { it.readBytes() }
                conn.disconnect()
            } catch (e: Exception) {
                Log.d(TAG, "上报失败: " + e.message)
            }
        }
    }
}
