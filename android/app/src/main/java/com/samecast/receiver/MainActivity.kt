package com.samecast.receiver

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.AudioPlaybackConfiguration
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
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
import java.util.UUID

/**
 * 全屏接收端：电视上只显示画面，没有播放器 UI。
 * 遥控器「确定 / 播放暂停」会作为全组暂停、播放指令上报给网关。
 */
class MainActivity : Activity() {

    companion object {
        private const val TAG = "samecast"
        /** 同时注册的媒体播放达到几路才判定被接管（2 = 我们自己 + 另一路）。 */
        private const val FOREIGN_PLAYBACK_MIN = 2
    }

    private lateinit var player: ExoPlayer
    private lateinit var playerView: PlayerView
    private lateinit var statusView: TextView
    private lateinit var client: GatewayClient
    private val ui = Handler(Looper.getMainLooper())
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_main)

        playerView = findViewById(R.id.player_view)
        statusView = findViewById(R.id.status)

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
                report(true)
            }

            override fun onPlaybackStateChanged(playbackState: Int) {
                report(true)
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.w(TAG, "播放失败: " + error.errorCodeName)
                showStatus("播放失败: " + error.errorCodeName + "（直链失效或被防盗链拦截）")
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
        client = GatewayClient(
            receiverId = receiverId,
            deviceName = tvName,
            onCommand = { cmd -> ui.post { applyCommand(cmd) } },
            onStatus = { text -> ui.post { showStatus(text) } },
        )
        client.start()

        ui.postDelayed(reportTick, 800)
        report(true)
    }

    private val reportTick = object : Runnable {
        override fun run() {
            report(false)
            watchForeignPlayback()
            ui.postDelayed(this, 800)
        }
    }

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
        if (preempted || !player.isPlaying) {
            foreignSeen = 0
            return
        }
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
        statusView.text = "已被其他投屏接管，SameCast 已让出画面"
        statusView.visibility = View.VISIBLE
        ui.removeCallbacks(hideStatus)
        report(true)
    }

    /** 回到 SameCast（网关下发 load/play 或遥控器播放）：撤销让位状态。 */
    private fun reclaimScreen() {
        if (!preempted) return
        preempted = false
        Log.i(TAG, "重新接管画面")
        requestAudioFocus()
    }

    private val hideStatus = Runnable {
        if (player.isPlaying) statusView.visibility = View.GONE
    }

    private fun showStatus(text: String) {
        statusView.text = text
        statusView.visibility = View.VISIBLE
        ui.removeCallbacks(hideStatus)
        ui.postDelayed(hideStatus, 2500)
    }

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

    private fun report(force: Boolean) {
        val now = System.currentTimeMillis()
        if (!force && now - lastReport < 400) return
        lastReport = now
        val dur = player.duration
        client.report(
            currentState(), positionSeconds(),
            if (dur > 0) dur / 1000.0 else 0.0, preempted,
        )
    }

    private fun applyCommand(cmd: JSONObject) {
        when (cmd.optString("cmd")) {
            "load" -> {
                val uri = cmd.optString("uri")
                if (uri.isNotEmpty()) {
                    reclaimScreen()
                    player.setMediaItem(MediaItem.fromUri(uri))
                    player.prepare()
                    ensureForeground()
                    showStatus("载入: " + uri.take(60))
                }
            }
            "play" -> {
                reclaimScreen()
                wantPlaying = true
                requestAudioFocus()
                player.play()
                ensureForeground()
                showStatus("播放（与整组同步）")
            }
            "pause" -> {
                wantPlaying = false
                player.pause()
                abandonAudioFocus()
                showStatus("暂停（与整组同步）")
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
                if (preempted) {
                    statusView.text = "已被其他投屏接管，SameCast 已让出画面"
                    statusView.visibility = View.VISIBLE
                    ui.removeCallbacks(hideStatus)
                } else {
                    showStatus("已停止")
                }
            }
            "volume" -> player.volume = (cmd.optDouble("volume", 50.0) / 100.0).toFloat()
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
                val position = positionSeconds()
                if (player.isPlaying) {
                    wantPlaying = false
                    player.pause()
                    abandonAudioFocus()
                    client.sendCommand(JSONObject().put("cmd", "pause").put("position", position))
                    showStatus("本机暂停，其他电视同步暂停")
                } else {
                    // 遥控器主动回到 SameCast：撤销让位，重新抢焦点和前台
                    reclaimScreen()
                    wantPlaying = true
                    requestAudioFocus()
                    player.play()
                    ensureForeground()
                    client.sendCommand(JSONObject().put("cmd", "play").put("position", position))
                    showStatus("本机播放，其他电视同步播放")
                }
                report(true)
                return true
            }
        }
        return super.onKeyDown(keyCode, event)
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

    override fun onDestroy() {
        abandonAudioFocus()
        client.stop()
        player.release()
        super.onDestroy()
    }
}
