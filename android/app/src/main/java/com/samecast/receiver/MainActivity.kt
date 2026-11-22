package com.samecast.receiver

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.AudioPlaybackConfiguration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.ProgressBar
import android.widget.TextView
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import org.json.JSONObject
import java.util.Locale
import java.util.UUID

/**
 * 电视端接收界面，尽量贴近电视自带投屏的观感：
 *
 *  待机首页 → 品牌/设备名/投屏指引/网关连接状态
 *  播放中   → 全屏画面，遥控器「确定」呼出顶部信息条（片名/状态/进度）
 *  暂停     → 居中提示"已暂停，在手机上点播放即可继续"
 *  被抢占   → 提示已被其他投屏接管
 *  失败     → 提示播放失败
 *
 * 同步逻辑都交给网关：本机只负责放画面、上报进度、执行指令。
 */
class MainActivity : Activity() {

    companion object {
        private const val TAG = "samecast"
        /** 同时注册的媒体播放达到几路才判定被接管（2 = 我们自己 + 另一路）。 */
        private const val FOREIGN_PLAYBACK_MIN = 2
        private const val HUD_TIMEOUT = 5000L
        private const val TOAST_TIMEOUT = 2600L
        private val TITLE_RE = Regex("<dc:title[^>]*>(.*?)</dc:title>", RegexOption.DOT_MATCHES_ALL)
    }

    // ---- 播放器 ----
    private lateinit var player: ExoPlayer
    private lateinit var playerView: PlayerView
    private lateinit var client: GatewayClient
    private val ui = Handler(Looper.getMainLooper())

    // ---- 界面 ----
    private lateinit var idlePanel: View
    private lateinit var idleDevice: TextView
    private lateinit var gatewayStatus: TextView
    private lateinit var gatewayDot: View
    private lateinit var loadingSpinner: View
    private lateinit var overlayPanel: View
    private lateinit var overlayTitle: TextView
    private lateinit var overlayHint: TextView
    private lateinit var hudPanel: View
    private lateinit var hudTitle: TextView
    private lateinit var hudState: TextView
    private lateinit var hudProgress: ProgressBar
    private lateinit var toastView: TextView

    // ---- 状态 ----
    private var lastReport = 0L
    private var receiverId: String = ""
    private var resumed = false
    private lateinit var audioManager: AudioManager
    /** 已经被别的投屏接管，让出画面；在网关发来新的 load/play 之前不再抢回前台。 */
    private var preempted = false
    /** 网关最后一次要求的是"播放"状态。 */
    private var wantPlaying = false
    /** 当前是否持有音频焦点（避免重复申请）。 */
    private var hasFocus = false
    /** 连续检测到"多路媒体在播"的次数，用于过滤瞬时抖动。 */
    private var foreignSeen = 0
    private var preemptedIdleSeen = 0
    /** 当前片子（从 DLNA 元数据里取 dc:title），信息条上显示。 */
    private var mediaTitle = ""
    /** 播放失败提示；有新载入或停止时清空。 */
    private var errorText: String? = null
    private var hudVisible = false
    private var gatewayOnline = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_main)

        playerView = findViewById(R.id.player_view)
        idlePanel = findViewById(R.id.idle_panel)
        idleDevice = findViewById(R.id.idle_device)
        gatewayStatus = findViewById(R.id.gateway_status)
        gatewayDot = findViewById(R.id.gateway_dot)
        loadingSpinner = findViewById(R.id.loading_spinner)
        overlayPanel = findViewById(R.id.overlay_panel)
        overlayTitle = findViewById(R.id.overlay_title)
        overlayHint = findViewById(R.id.overlay_hint)
        hudPanel = findViewById(R.id.hud_panel)
        hudTitle = findViewById(R.id.hud_title)
        hudState = findViewById(R.id.hud_state)
        hudProgress = findViewById(R.id.hud_progress)
        toastView = findViewById(R.id.toast_view)

        val versionName = try {
            packageManager.getPackageInfo(packageName, 0).versionName
        } catch (e: Exception) {
            ""
        }
        findViewById<TextView>(R.id.version).text = "SameCast v" + versionName

        audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        player = ExoPlayer.Builder(this).build()
        // 音频焦点自己管（不让 ExoPlayer 自动处理）：
        // 电视自带的投屏界面 / 其他投屏 App 开始播放时会抢走焦点，
        // 我们收到永久性丢失就把画面让出去，既不会回声也不会和对方来回抢。
        player.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MOVIE)
                .build(),
            /* handleAudioFocus = */ false,
        )
        playerView.player = player
        playerView.useController = false
        playerView.resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
        player.addListener(object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                refreshUi()
                report(true)
            }

            override fun onPlaybackStateChanged(playbackState: Int) {
                refreshUi()
                report(true)
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.w(TAG, "播放失败: " + error.errorCodeName)
                errorText = "片源加载失败（" + error.errorCodeName + "）\n" +
                    "可能是直链过期或被防盗链拦截，在手机上重新投屏试试"
                refreshUi()
            }
        })

        val prefs = getSharedPreferences("receiver", MODE_PRIVATE)
        receiverId = prefs.getString("receiver_id", null)
            ?: UUID.randomUUID().toString().also { prefs.edit().putString("receiver_id", it).apply() }

        val model = (Build.MODEL ?: "").trim()
        val brand = (Build.BRAND ?: "").trim()
        val tvName = when {
            model.isEmpty() -> "电视"
            brand.isEmpty() || model.startsWith(brand, ignoreCase = true) -> model
            else -> brand.replaceFirstChar { it.uppercase() } + " " + model
        }
        idleDevice.text = tvName

        client = GatewayClient(
            receiverId = receiverId,
            deviceName = tvName,
            onCommand = { cmd -> ui.post { applyCommand(cmd) } },
            onStatus = { text -> ui.post { setGatewayStatus(text) } },
        )
        client.start()

        ui.postDelayed(reportTick, 800)
        report(true)
        refreshUi()
    }

    private val reportTick = object : Runnable {
        override fun run() {
            report(false)
            watchForeignPlayback()
            refreshUi()
            ui.postDelayed(this, 800)
        }
    }

    // ---- 界面刷新 ----

    private fun refreshUi() {
        if (!::player.isInitialized) return
        val hasMedia = player.mediaItemCount > 0
        val buffering = hasMedia && player.playbackState == Player.STATE_BUFFERING
        val ended = hasMedia && player.playbackState == Player.STATE_ENDED
        val playing = hasMedia && player.isPlaying
        val paused = hasMedia && !playing && !buffering && !ended && !preempted &&
            player.playbackState != Player.STATE_IDLE

        idlePanel.visibility = if (!hasMedia && !preempted) View.VISIBLE else View.GONE
        loadingSpinner.visibility = if (buffering && !preempted) View.VISIBLE else View.GONE

        when {
            preempted -> {
                overlayTitle.text = "已被其他投屏接管"
                overlayHint.text = "SameCast 已让出画面\n在手机上重新选择 SameCast，即可继续同步投屏"
                overlayPanel.visibility = View.VISIBLE
            }
            errorText != null -> {
                overlayTitle.text = "播放失败"
                overlayHint.text = errorText
                overlayPanel.visibility = View.VISIBLE
            }
            ended -> {
                // 播到片尾：和自带投屏一样，标题下写明进度和下一步
                overlayTitle.text = mediaTitle.ifEmpty { "播放结束" }
                overlayHint.text = "播放结束" + progressLine() +
                    "\n在手机上选择下一集，或重新投屏到 SameCast"
                overlayPanel.visibility = View.VISIBLE
            }
            paused -> {
                // 暂停页按自带投屏的做法：片名 + 进度 + 操作提示
                overlayTitle.text = mediaTitle.ifEmpty { "已暂停" }
                overlayHint.text = "已暂停" + progressLine() +
                    "\n按遥控器「确定」，或在手机上点播放\n所有电视会一起继续"
                overlayPanel.visibility = View.VISIBLE
            }
            else -> overlayPanel.visibility = View.GONE
        }

        if (hudVisible) {
            setHudContent()
            hudPanel.visibility = if (hasMedia) View.VISIBLE else View.GONE
        } else {
            hudPanel.visibility = View.GONE
        }
    }

    private fun setHudContent() {
        hudTitle.text = if (mediaTitle.isNotEmpty()) mediaTitle else "SameCast 同步投屏"
        val pos = positionSeconds()
        val dur = durationSeconds()
        val stateText = when {
            player.playbackState == Player.STATE_BUFFERING -> "缓冲中…"
            player.isPlaying -> "正在播放"
            player.playbackState == Player.STATE_ENDED -> "播放结束"
            player.mediaItemCount > 0 -> "已暂停"
            else -> "等待投屏"
        }
        hudState.text = if (dur > 0) {
            stateText + " · " + fmtTime(pos) + " / " + fmtTime(dur)
        } else {
            stateText
        }
        hudProgress.progress = if (dur > 0) {
            ((pos / dur) * 1000).toInt().coerceIn(0, 1000)
        } else {
            0
        }
    }

    private val hideHud = Runnable {
        hudVisible = false
        hudPanel.visibility = View.GONE
    }

    private fun showHud() {
        if (!::hudPanel.isInitialized) return
        if (player.mediaItemCount == 0) return
        hudVisible = true
        setHudContent()
        hudPanel.visibility = View.VISIBLE
        ui.removeCallbacks(hideHud)
        ui.postDelayed(hideHud, HUD_TIMEOUT)
    }

    private val hideToast = Runnable { toastView.visibility = View.GONE }

    private fun showToast(text: String) {
        if (!::toastView.isInitialized) return
        toastView.text = text
        toastView.visibility = View.VISIBLE
        ui.removeCallbacks(hideToast)
        ui.postDelayed(hideToast, TOAST_TIMEOUT)
    }

    /** 网关连接状态：待机首页上的一行小字 + 状态灯。 */
    private fun setGatewayStatus(text: String) {
        val connected = text.contains("已连接")
        val lost = text.contains("没找到") || text.contains("确认")
        gatewayStatus.text = when {
            connected -> "已连接 SameCast 网关"
            lost -> "未找到网关 · 请确认电视和运行服务的电脑在同一局域网"
            else -> "正在查找 SameCast 网关…"
        }
        gatewayDot.setBackgroundResource(
            when {
                connected -> R.drawable.dot_on
                lost -> R.drawable.dot_off
                else -> R.drawable.dot_warn
            }
        )
        // 连接状态变化时给一句提示：投屏中途断开会让人一头雾水
        when {
            connected && !gatewayOnline -> showToast("已连接 SameCast 网关")
            !connected && gatewayOnline && player.mediaItemCount > 0 ->
                showToast("SameCast 网关已断开，画面继续播放\n恢复连接后会自动重新同步")
        }
        gatewayOnline = connected
    }

    /** 暂停 / 结束提示里的进度行，例如 " · 06:42 / 18:49"。 */
    private fun progressLine(): String {
        val dur = durationSeconds()
        if (dur <= 0) return ""
        return " · " + fmtTime(positionSeconds()) + " / " + fmtTime(dur)
    }

    private fun fmtTime(seconds: Double): String {
        val total = seconds.toLong().coerceAtLeast(0L)
        val h = total / 3600
        val m = (total % 3600) / 60
        val s = total % 60
        return if (h > 0) {
            String.format(Locale.US, "%d:%02d:%02d", h, m, s)
        } else {
            String.format(Locale.US, "%02d:%02d", m, s)
        }
    }

    /** 从 DLNA 元数据里取片名（手机投屏时会带 dc:title）。 */
    private fun titleFromMetadata(metadata: String): String {
        if (metadata.isEmpty()) return ""
        val raw = TITLE_RE.find(metadata)?.groupValues?.get(1)?.trim() ?: return ""
        return raw
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", "\"")
            .replace("&#39;", "'")
            .replace("&amp;", "&")
    }

    private fun guessTitle(uri: String): String {
        val name = uri.substringBefore('?').substringAfterLast('/')
        return try {
            if (name.isNotEmpty()) Uri.decode(name) else ""
        } catch (e: Exception) {
            ""
        }
    }

    // ---- 网关指令 / 状态上报 ----

    private fun currentState(): String = when {
        player.playbackState == Player.STATE_BUFFERING -> "buffering"
        player.isPlaying -> "playing"
        player.playbackState == Player.STATE_ENDED -> "stopped"
        player.currentPosition > 0 -> "paused"
        else -> "idle"
    }

    /** 当前进度(秒)：钳制在媒体时长内，避免 seek 越界后把目标位置当进度上报。 */
    private fun positionSeconds(): Double {
        val pos = player.currentPosition.coerceAtLeast(0L)
        val dur = player.duration
        val clamped = if (dur > 0 && pos > dur) dur else pos
        return clamped / 1000.0
    }

    private fun durationSeconds(): Double {
        val dur = player.duration
        return if (dur > 0 && dur != C.TIME_UNSET) dur / 1000.0 else 0.0
    }

    private fun report(force: Boolean) {
        val now = System.currentTimeMillis()
        if (!force && now - lastReport < 400) return
        lastReport = now
        client.report(
            currentState(), positionSeconds(), durationSeconds(), preempted,
        )
    }

    private fun applyCommand(cmd: JSONObject) {
        when (cmd.optString("cmd")) {
            "load" -> {
                val uri = cmd.optString("uri")
                if (uri.isNotEmpty()) {
                    reclaimScreen()
                    errorText = null
                    mediaTitle = titleFromMetadata(cmd.optString("metadata")).ifEmpty { guessTitle(uri) }
                    player.setMediaItem(MediaItem.fromUri(uri))
                    player.prepare()
                    ensureForeground()
                    showToast("正在载入片源…")
                    showHud()
                    refreshUi()
                }
            }
            "play" -> {
                reclaimScreen()
                errorText = null
                wantPlaying = true
                requestAudioFocus()
                player.play()
                ensureForeground()
                showToast("正在同步播放")
                refreshUi()
            }
            "pause" -> {
                wantPlaying = false
                player.pause()
                abandonAudioFocus()
                refreshUi()
            }
            "seek" -> {
                // 目标进度不能超过媒体时长，否则播放器会直接进入结束态
                var target = (cmd.optDouble("position", 0.0) * 1000).toLong().coerceAtLeast(0L)
                val dur = player.duration
                if (dur > 0 && target > dur) target = (dur - 500L).coerceAtLeast(0L)
                player.seekTo(target)
            }
            "stop" -> {
                wantPlaying = false
                player.stop()
                player.clearMediaItems()
                abandonAudioFocus()
                mediaTitle = ""
                errorText = null
                if (!preempted) showToast("已停止")
                refreshUi()
            }
            "volume" -> {
                val volume = (cmd.optDouble("volume", 50.0) / 100.0).toFloat().coerceIn(0f, 1f)
                player.volume = volume
                showToast("音量 " + Math.round(volume * 100) + "%")
            }
            "speed" -> {
                // 小偏差用倍速微调，避免 seek 重新缓冲导致卡顿
                val sp = cmd.optDouble("speed", 1.0).toFloat().coerceIn(0.5f, 2.0f)
                player.setPlaybackSpeed(sp)
            }
        }
        report(true)
    }

    /** 遥控器操作：本机暂停、播放 -> 上报网关，由网关广播给其他电视。 */
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        when (keyCode) {
            KeyEvent.KEYCODE_DPAD_CENTER,
            KeyEvent.KEYCODE_SPACE,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_MEDIA_PLAY,
            KeyEvent.KEYCODE_MEDIA_PAUSE -> {
                if (errorText != null && player.mediaItemCount > 0) {
                    // 播放失败时按「确定」= 重试，不用回手机重新投屏
                    errorText = null
                    player.prepare()
                    showToast("正在重试…")
                    refreshUi()
                    return true
                }
                if (player.mediaItemCount > 0) showHud()
                val position = positionSeconds()
                if (player.isPlaying) {
                    wantPlaying = false
                    player.pause()
                    abandonAudioFocus()
                    client.sendCommand(JSONObject().put("cmd", "pause").put("position", position))
                    showToast("本机暂停，其他电视同步暂停")
                } else {
                    // 遥控器主动回到 SameCast：撤销让位，重新抢焦点和前台
                    reclaimScreen()
                    wantPlaying = true
                    requestAudioFocus()
                    player.play()
                    ensureForeground()
                    client.sendCommand(JSONObject().put("cmd", "play").put("position", position))
                    showToast("本机播放，其他电视同步播放")
                }
                report(true)
                refreshUi()
                return true
            }
            KeyEvent.KEYCODE_DPAD_LEFT,
            KeyEvent.KEYCODE_MEDIA_REWIND -> {
                if (player.mediaItemCount > 0) {
                    seekBy(-15.0)
                    return true
                }
            }
            KeyEvent.KEYCODE_DPAD_RIGHT,
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> {
                if (player.mediaItemCount > 0) {
                    seekBy(15.0)
                    return true
                }
            }
            KeyEvent.KEYCODE_DPAD_UP,
            KeyEvent.KEYCODE_DPAD_DOWN,
            KeyEvent.KEYCODE_INFO,
            KeyEvent.KEYCODE_MENU -> {
                if (player.mediaItemCount > 0) {
                    showHud()
                    return true
                }
            }
        }
        return super.onKeyDown(keyCode, event)
    }

    /**
     * 遥控器快进/快退：本机 seek，并让网关把整组拉到同一个进度。
     * 只在所有电视都对齐的前提下快进，否则会各走各的。
     */
    private fun seekBy(delta: Double) {
        if (player.mediaItemCount == 0) return
        var target = ((positionSeconds() + delta).coerceAtLeast(0.0) * 1000).toLong()
        val dur = player.duration
        if (dur > 0 && target > dur - 500) target = (dur - 500L).coerceAtLeast(0L)
        player.seekTo(target)
        showHud()
        showToast(if (delta > 0) "快进 " + delta.toInt() + " 秒" else "快退 " + (-delta).toInt() + " 秒")
        client.sendCommand(JSONObject().put("cmd", "seek").put("position", target / 1000.0))
        report(true)
    }

    /** 返回键：退回电视主页，接收端进程继续在线（手机还能接着投），不会掉线。 */
    override fun onBackPressed() {
        moveTaskToBack(true)
    }

    override fun onResume() {
        super.onResume()
        resumed = true
    }

    override fun onPause() {
        resumed = false
        super.onPause()
    }

    /**
     * 退到后台（别的投屏界面盖上来、或电视回到主页）：先停下，不在后台出声。
     * 这是正常暂停，整组跟着暂停；位置不会丢，手机上点播放即可恢复。
     */
    override fun onStop() {
        super.onStop()
        resumed = false
        if (!preempted && (player.isPlaying || wantPlaying)) {
            Log.i(TAG, "退到后台，暂停播放")
            wantPlaying = false
            player.pause()
            abandonAudioFocus()
            report(true)
        }
    }

    /** 收到播放指令时把接收端拉回前台：电视自带投屏界面还开着的话会盖住画面。 */
    private fun ensureForeground() {
        if (resumed) return
        val intent = Intent(this, MainActivity::class.java).addFlags(
            Intent.FLAG_ACTIVITY_REORDER_TO_FRONT or Intent.FLAG_ACTIVITY_NEW_TASK
        )
        try {
            startActivity(intent)
        } catch (e: Exception) {
            Log.w(TAG, "回到前台失败: " + e.message)
        }
    }

    // ---- 音频焦点 / 被抢占时让出画面 ----

    /** 音频焦点回调：永久丢失说明别的投屏/播放器开始放声音了，让位。 */
    private val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
        Log.i(TAG, "音频焦点变化: " + change)
        hasFocus = change == AudioManager.AUDIOFOCUS_GAIN
        if (change == AudioManager.AUDIOFOCUS_LOSS) {
            ui.post { yieldToOtherCast("音频焦点被其他应用抢走") }
        }
    }

    /**
     * 有的厂商投屏（电视自带 DLNA 渲染器）在后台播放、不抢音频焦点，光靠焦点回调发现不了。
     * API 26+ 直接看系统里同时有几路媒体在播：我们自己在放 + 另一路也在放 = 被别人接管了。
     */
    private fun watchForeignPlayback() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val configs = try {
            audioManager.activePlaybackConfigurations
        } catch (e: Exception) {
            return
        }
        // 公开 SDK 只给得到 usage（状态是隐藏 API）：同一 TV 上同时注册的媒体播放多于一路，
        // 说明除我们之外还有播放器在放。
        val playing = configs.count { c ->
            val usage = try {
                c.audioAttributes.usage
            } catch (e: Exception) {
                -1
            }
            usage == android.media.AudioAttributes.USAGE_MEDIA ||
                usage == android.media.AudioAttributes.USAGE_GAME
        }
        if (preempted) {
            // 别的投屏结束了：像原生投屏一样自动退回待机页，别把「已让出」停在屏幕上。
            if (player.mediaItemCount > 0) {
                preemptedIdleSeen = 0
                return
            }
            preemptedIdleSeen = if (playing == 0) preemptedIdleSeen + 1 else 0
            if (preemptedIdleSeen >= 6) {
                preemptedIdleSeen = 0
                Log.i(TAG, "其他投屏已结束，回到待机页")
                preempted = false
                refreshUi()
            }
            return
        }
        if (!player.isPlaying) {
            foreignSeen = 0
            return
        }
        // 连续两次采样都多于一路，才判定为真的被别的播放器抢占
        foreignSeen = if (playing >= FOREIGN_PLAYBACK_MIN) foreignSeen + 1 else 0
        if (foreignSeen >= 2) {
            foreignSeen = 0
            Log.i(TAG, "检测到 " + playing + " 路媒体在播放，让出画面")
            yieldToOtherCast("检测到其他应用在播放（可能是别的投屏）")
        }
    }

    private fun requestAudioFocus(): Boolean {
        if (hasFocus) return true
        val result = audioManager.requestAudioFocus(
            focusListener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN,
        )
        hasFocus = result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        return hasFocus
    }

    private fun abandonAudioFocus() {
        hasFocus = false
        audioManager.abandonAudioFocus(focusListener)
    }

    /**
     * 被其他投屏接管：停下播放、清掉媒体，并上报 preempted 让网关整组让位。
     * 之后不再主动抢前台，直到网关下发新的 load/play。
     */
    private fun yieldToOtherCast(reason: String) {
        if (preempted) return
        preempted = true
        wantPlaying = false
        Log.i(TAG, "被其他投屏接管: " + reason)
        player.pause()
        player.stop()
        player.clearMediaItems()
        abandonAudioFocus()
        mediaTitle = ""
        refreshUi()
        report(true)
    }

    /** 回到 SameCast（网关下发 load/play 或遥控器播放）：撤销让位状态。 */
    private fun reclaimScreen() {
        if (!preempted) return
        preempted = false
        Log.i(TAG, "重新接管画面")
        requestAudioFocus()
        refreshUi()
    }

    override fun onDestroy() {
        ui.removeCallbacksAndMessages(null)
        abandonAudioFocus()
        client.stop()
        player.release()
        super.onDestroy()
    }
}
