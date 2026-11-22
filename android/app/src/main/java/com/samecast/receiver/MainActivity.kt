package com.samecast.receiver

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Typeface
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.Format
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.exoplayer.trackselection.DefaultTrackSelector
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import org.json.JSONObject
import java.util.Locale
import java.util.UUID

/**
 * 电视端接收界面，观感对齐 YouTube / 哔哩哔哩电视版：
 *
 *  待机首页 → 品牌/设备名/投屏指引/网关连接状态
 *  播放中   → 全屏画面；顶部片名 + 「来自手机投屏 · N 台电视同步」，底部控制栏（进度/播放/快退快进/
 *             音量/同步/清晰度/更多），4 秒无操作自动收起，暂停时不收起
 *  暂停     → 居中「已暂停」+ 控制栏同时可见
 *  同步     → 同步面板显示每台电视与整组的偏差，「立即对齐」让网关把所有电视拉回同一时刻
 *  清晰度   → 自动 / 1080p / 720p / 480p，切换立即生效并保存
 *  被抢占   → 提示已被其他投屏接管
 *  失败     → 提示无法播放这个视频
 *
 * 同步逻辑都交给网关：本机只负责放画面、上报进度、执行指令。
 */
class MainActivity : Activity() {

    companion object {
        private const val TAG = "samecast"
        /** 同时注册的媒体播放达到几路才判定被接管（2 = 我们自己 + 另一路）。 */
        private const val FOREIGN_PLAYBACK_MIN = 2
        /** 控制栏自动收起时间：开播后 4 秒；暂停 / 面板打开时不收。 */
        private const val BAR_TIMEOUT = 4000L
        private const val TOAST_TIMEOUT = 3000L
        private const val VOLUME_TIMEOUT = 2600L
        /** 同步面板打开时每 500ms 刷新一次整组状态。 */
        private const val SYNC_POLL_MS = 500L
        /** 遥控器快进/快退步长（秒）。 */
        private const val SEEK_STEP = 10.0
        /** 与整组偏差小于 150ms 视作对齐。 */
        private const val DEVIATION_OK_MS = 150

        private const val BUTTON_REWIND = 0
        private const val BUTTON_PLAY = 1
        private const val BUTTON_FORWARD = 2
        private const val BUTTON_VOLUME = 3
        private const val BUTTON_SYNC = 4
        private const val BUTTON_QUALITY = 5
        private const val BUTTON_MORE = 6

        /** B 站 CDN 会校验 Referer，只对 B 站域名补这个头，别的投屏源一律不动。 */
        private const val BILI_REFERER = "https://www.bilibili.com/"
        private const val BILI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        private const val PREFS = "receiver"
        private const val KEY_QUALITY = "quality_height"

        private val TITLE_RE = Regex("<dc:title[^>]*>(.*?)</dc:title>", RegexOption.DOT_MATCHES_ALL)
    }

    // ---- 播放器 ----
    private lateinit var player: ExoPlayer
    private lateinit var trackSelector: DefaultTrackSelector
    private lateinit var playerView: PlayerView
    private lateinit var client: GatewayClient
    private val ui = Handler(Looper.getMainLooper())

    // ---- 待机首页 ----
    private lateinit var idlePanel: View
    private lateinit var idleDevice: TextView
    private lateinit var gatewayStatus: TextView
    private lateinit var gatewayDot: View
    private lateinit var versionText: TextView

    // ---- 加载 / 居中提示 ----
    private lateinit var loadingPanel: View
    private lateinit var overlayPanel: View
    private lateinit var overlayIcon: View
    private lateinit var overlayTitle: TextView
    private lateinit var overlayHint: TextView

    // ---- 顶部片名信息 ----
    private lateinit var topInfoPanel: View
    private lateinit var topTitle: TextView
    private lateinit var topDot: View
    private lateinit var topSyncText: TextView
    private lateinit var topChip: TextView

    // ---- 底部控制栏 ----
    private lateinit var bottomBar: View
    private lateinit var controlPosition: TextView
    private lateinit var controlDuration: TextView
    private lateinit var progressTrack: View
    private lateinit var progressFill: View
    private lateinit var progressThumb: View
    private lateinit var btnPlayIcon: ImageView
    private lateinit var barButtons: List<View>

    // ---- 面板 ----
    private lateinit var syncPanel: View
    private lateinit var syncRows: LinearLayout
    private lateinit var syncEmpty: TextView
    private lateinit var btnResync: TextView
    private lateinit var btnSyncClose: TextView
    private lateinit var qualityPanel: View
    private lateinit var qualityRows: LinearLayout
    private lateinit var qualityNow: TextView
    private lateinit var morePanel: View
    private lateinit var moreRows: LinearLayout
    private lateinit var moreVersion: TextView

    // ---- 轻提示 / 音量 ----
    private lateinit var toastView: TextView
    private lateinit var volumeHud: TextView

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
    private var gatewayOnline = false
    private var appVersion = ""

    // 控制栏状态
    private var barVisible = false
    /** 控制栏内当前高亮按钮；-1 = 控制栏未展开（此时左右键是快退/快进，而不是选按钮）。 */
    private var barFocus = -1

    /** 打开的面板："" / "sync" / "quality" / "more" / "volume"。 */
    private var panel = ""
    private val panelFocusables = ArrayList<View>()
    private var panelFocusIndex = 0

    /** 清晰度上限高度：0 = 自动（不限制）。 */
    private var qualityHeight = 0

    // 同步面板数据
    private var tvCount = 1
    /** 当前这条投屏的来源（phone = 手机投屏，bili = B 站小电视），用来显示顶部一行。 */
    private var castSource = ""
    private var maxDeviationMs = 0
    private var syncFetching = false
    /** 上一次后台刷新整组信息（台数/偏差）的时间，控制栏打开时低频拉取。 */
    private var lastStatusFetch = 0L

    // 轻提示队列：同一时刻只显示一条
    private val toastQueue = ArrayList<String>()
    private var toastShowing = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_main)
        bindViews()

        appVersion = try {
            packageManager.getPackageInfo(packageName, 0).versionName
        } catch (e: Exception) {
            ""
        }
        versionText.text = "SameCast v" + appVersion
        moreVersion.text = "SameCast " + appVersion + " · 接收端"

        audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        qualityHeight = prefs.getInt(KEY_QUALITY, 0)

        trackSelector = DefaultTrackSelector(this)
        // B 站 CDN 校验 Referer：只对 B 站的域名补请求头，其他投屏源保持原样
        val httpFactory = DefaultHttpDataSource.Factory()
        val headers = mapOf(
            "Referer" to BILI_REFERER,
            "User-Agent" to BILI_UA,
            "Accept" to "*/*",
        )
        val biliAware = ResolvingDataSource.Factory(httpFactory) { spec ->
            val host = spec.uri.host ?: ""
            if (host.contains("bilivideo") || host.contains("bilibili")) {
                spec.withRequestHeaders(headers)
            } else {
                spec
            }
        }
        player = ExoPlayer.Builder(this)
            .setTrackSelector(trackSelector)
            .setMediaSourceFactory(DefaultMediaSourceFactory(this).setDataSourceFactory(biliAware))
            .build()
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
        applyQuality()

        player.addListener(object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                refreshUi()
                refreshControlBar()
                resetBarTimer(isPlaying)
                report(true)
            }

            override fun onPlaybackStateChanged(playbackState: Int) {
                refreshUi()
                refreshControlBar()
                report(true)
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.w(TAG, "播放失败: " + error.errorCodeName)
                errorText = "片源加载失败（" + error.errorCodeName + "）\n" +
                    "可能是直链过期或被防盗链拦截，在手机上重新投屏试试"
                refreshUi()
            }
        })

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

        btnResync.setOnClickListener { resyncNow() }
        btnSyncClose.setOnClickListener { closePanel() }

        ui.postDelayed(reportTick, 800)
        report(true)
        refreshUi()
    }

    private fun bindViews() {
        playerView = findViewById(R.id.player_view)
        idlePanel = findViewById(R.id.idle_panel)
        idleDevice = findViewById(R.id.idle_device)
        gatewayStatus = findViewById(R.id.gateway_status)
        gatewayDot = findViewById(R.id.gateway_dot)
        versionText = findViewById(R.id.version)

        loadingPanel = findViewById(R.id.loading_panel)
        overlayPanel = findViewById(R.id.overlay_panel)
        overlayIcon = findViewById(R.id.overlay_icon)
        overlayTitle = findViewById(R.id.overlay_title)
        overlayHint = findViewById(R.id.overlay_hint)

        topInfoPanel = findViewById(R.id.top_info_panel)
        topTitle = findViewById(R.id.top_title)
        topDot = findViewById(R.id.top_dot)
        topSyncText = findViewById(R.id.top_sync_text)
        topChip = findViewById(R.id.top_chip)

        bottomBar = findViewById(R.id.bottom_control_panel)
        controlPosition = findViewById(R.id.control_position)
        controlDuration = findViewById(R.id.control_duration)
        progressTrack = findViewById(R.id.progress_track)
        progressFill = findViewById(R.id.progress_fill)
        progressThumb = findViewById(R.id.progress_thumb)
        btnPlayIcon = findViewById(R.id.btn_play_icon)
        barButtons = listOf(
            findViewById(R.id.btn_rewind),
            findViewById(R.id.btn_play),
            findViewById(R.id.btn_forward),
            findViewById(R.id.btn_volume),
            findViewById(R.id.btn_sync),
            findViewById(R.id.btn_quality),
            findViewById(R.id.btn_more),
        )
        // 控制栏焦点由代码控制（isActivated 画焦点框），不交给系统焦点：
        // 这样焦点不会跑到画面外，左右键在控制栏展开时只用来选按钮。
        for (button in barButtons) {
            button.isFocusable = false
            button.isClickable = false
        }

        syncPanel = findViewById(R.id.sync_panel)
        syncRows = findViewById(R.id.sync_rows)
        syncEmpty = findViewById(R.id.sync_empty)
        btnResync = findViewById(R.id.btn_resync)
        btnSyncClose = findViewById(R.id.btn_sync_close)
        btnResync.isFocusable = true
        btnResync.isClickable = true
        btnSyncClose.isFocusable = true
        btnSyncClose.isClickable = true

        qualityPanel = findViewById(R.id.quality_panel)
        qualityRows = findViewById(R.id.quality_rows)
        qualityNow = findViewById(R.id.quality_now)

        morePanel = findViewById(R.id.more_panel)
        moreRows = findViewById(R.id.more_rows)
        moreVersion = findViewById(R.id.more_version)

        toastView = findViewById(R.id.toast_view)
        volumeHud = findViewById(R.id.volume_hud)
    }

    // ---- 定时任务 ----

    private val reportTick = object : Runnable {
        override fun run() {
            report(false)
            watchForeignPlayback()
            refreshUi()
            if (barVisible) refreshProgress()
            // 控制栏打开时低频刷一遍整组信息：顶部「N 台电视同步」和同步状态才是准的
            if (barVisible && panel != "sync" && gatewayOnline) maybeRefreshGroupStatus()
            ui.postDelayed(this, 800)
        }
    }

    private val hideBarRunnable = Runnable { hideBarNow() }

    private val hideToastRunnable = Runnable { finishToast() }

    private val hideVolumeRunnable = Runnable {
        if (panel != "volume") volumeHud.visibility = View.GONE
    }

    private val syncTick = object : Runnable {
        override fun run() {
            if (panel != "sync") return
            fetchSyncStatus()
            ui.postDelayed(this, SYNC_POLL_MS)
        }
    }

    // ---- 界面刷新 ----

    private fun refreshUi() {
        if (!::player.isInitialized || !::idlePanel.isInitialized) return
        val hasMedia = player.mediaItemCount > 0
        val buffering = hasMedia && player.playbackState == Player.STATE_BUFFERING
        val ended = hasMedia && player.playbackState == Player.STATE_ENDED
        val playing = hasMedia && player.isPlaying
        val paused = hasMedia && !playing && !buffering && !ended && !preempted &&
            player.playbackState != Player.STATE_IDLE
        val panelOpen = panel.isNotEmpty()

        idlePanel.visibility = if (!hasMedia && !preempted) View.VISIBLE else View.GONE

        var overlayShown = false
        var overlayDim = 0x99000000.toInt()
        var showPausedIcon = false
        when {
            preempted -> {
                overlayTitle.text = "已被其他投屏接管"
                overlayHint.text = "SameCast 已让出画面\n在手机上重新选择 SameCast，即可继续同步投屏"
                overlayShown = true
            }
            errorText != null -> {
                overlayTitle.text = "无法播放这个视频"
                overlayHint.text = "在手机上重新投屏一次"
                overlayShown = true
            }
            ended -> {
                overlayTitle.text = "播放结束"
                overlayHint.text = "在手机上换一集，电视会自动跟随"
                overlayShown = true
            }
            paused -> {
                overlayTitle.text = "已暂停"
                overlayHint.text = "按「确定」继续 · 所有电视会一起继续"
                overlayShown = true
                overlayDim = 0x66000000.toInt()
                showPausedIcon = true
            }
        }
        overlayIcon.visibility = if (showPausedIcon) View.VISIBLE else View.GONE
        overlayPanel.setBackgroundColor(overlayDim)
        overlayPanel.visibility = if (overlayShown && !panelOpen) View.VISIBLE else View.GONE
        loadingPanel.visibility = if (buffering && !preempted && !panelOpen) View.VISIBLE else View.GONE

        when {
            !hasMedia -> {
                closePanelQuietly()
                hideBarNow()
            }
            preempted || errorText != null || ended -> {
                closePanelQuietly()
                hideBarNow()
            }
            paused && !panelOpen -> {
                if (!barVisible) showBar(autoHide = false)
            }
        }
        if (barVisible) refreshControlBar()
    }

    /** 控制栏/顶部信息的文字与进度。 */
    private fun refreshControlBar() {
        if (!::bottomBar.isInitialized) return
        topTitle.text = mediaTitle.ifEmpty { "SameCast 同步投屏" }
        updateTopSyncLine()
        controlPosition.text = fmtTime(positionSeconds())
        controlDuration.text = fmtTime(durationSeconds())
        btnPlayIcon.setImageResource(if (player.isPlaying) R.drawable.ic_pause else R.drawable.ic_play)
        refreshProgress()
    }

    /** 进度条填充 + 圆点位置。 */
    private fun refreshProgress() {
        if (!::progressTrack.isInitialized) return
        val dur = durationSeconds()
        val ratio = if (dur > 0) (positionSeconds() / dur).coerceIn(0.0, 1.0).toFloat() else 0f
        val width = progressTrack.width
        if (width <= 0) return
        val fillWidth = (width * ratio).toInt().coerceIn(0, width)
        val params = progressFill.layoutParams
        if (params.width != fillWidth) {
            params.width = fillWidth
            progressFill.layoutParams = params
        }
        val thumbWidth = progressThumb.width
        progressThumb.translationX = ((width - thumbWidth) * ratio).coerceAtLeast(0f)
    }

    /** 顶部第二行：「来自手机投屏/来自 B 站小电视 · N 台电视同步」+ 状态灯 + 同步状态小标签。 */
    private fun updateTopSyncLine() {
        val from = if (castSource == "bili") "来自 B 站小电视" else "来自手机投屏"
        topSyncText.text = from + " · " + tvCount.coerceAtLeast(1) + " 台电视同步"
        topChip.visibility = View.VISIBLE
        when {
            !gatewayOnline -> {
                topChip.text = "未连接网关"
                topChip.setTextColor(0xFFFBBF24.toInt())
                topDot.setBackgroundResource(R.drawable.dot_warn)
            }
            maxDeviationMs >= DEVIATION_OK_MS ||
                player.playbackState == Player.STATE_BUFFERING -> {
                topChip.text = "校准中"
                topChip.setTextColor(0xFF7CE0FF.toInt())
                topDot.setBackgroundResource(R.drawable.dot_warn)
            }
            else -> {
                topChip.text = "已同步"
                topChip.setTextColor(0xFF4ADE80.toInt())
                topDot.setBackgroundResource(R.drawable.dot_on)
            }
        }
    }

    // ---- 底部控制栏 ----

    private fun showBar(autoHide: Boolean, focusButton: Int = BUTTON_PLAY) {
        if (!::bottomBar.isInitialized) return
        if (player.mediaItemCount == 0) return
        barVisible = true
        bottomBar.visibility = View.VISIBLE
        topInfoPanel.visibility = View.VISIBLE
        setBarFocus(focusButton)
        refreshControlBar()
        bottomBar.post { refreshProgress() }
        resetBarTimer(autoHide)
    }

    private fun hideBarNow() {
        if (!::bottomBar.isInitialized) return
        barVisible = false
        ui.removeCallbacks(hideBarRunnable)
        bottomBar.visibility = View.GONE
        topInfoPanel.visibility = View.GONE
        setBarFocus(-1)
    }

    private fun resetBarTimer(autoHide: Boolean = player.isPlaying) {
        ui.removeCallbacks(hideBarRunnable)
        if (!barVisible || panel.isNotEmpty()) return
        if (autoHide && player.isPlaying) ui.postDelayed(hideBarRunnable, BAR_TIMEOUT)
    }

    private fun setBarFocus(index: Int) {
        if (!::barButtons.isInitialized) return
        barFocus = when {
            index < 0 -> -1
            index >= barButtons.size -> barButtons.size - 1
            else -> index
        }
        for (i in barButtons.indices) {
            barButtons[i].isActivated = i == barFocus
        }
    }

    private fun moveBarFocus(delta: Int) {
        setBarFocus(barFocus + delta)
        resetBarTimer()
    }

    private fun activateBarButton(index: Int) {
        when (index) {
            BUTTON_REWIND -> seekBy(-SEEK_STEP)
            BUTTON_PLAY -> togglePlayback()
            BUTTON_FORWARD -> seekBy(SEEK_STEP)
            BUTTON_VOLUME -> openPanel("volume")
            BUTTON_SYNC -> openPanel("sync")
            BUTTON_QUALITY -> openPanel("quality")
            BUTTON_MORE -> openPanel("more")
        }
    }

    // ---- 面板 ----

    private fun openPanel(name: String) {
        if (player.mediaItemCount == 0) return
        panel = name
        panelFocusIndex = 0
        ui.removeCallbacks(hideBarRunnable)
        bottomBar.visibility = View.GONE
        topInfoPanel.visibility = View.GONE
        overlayPanel.visibility = View.GONE
        loadingPanel.visibility = View.GONE

        syncPanel.visibility = if (name == "sync") View.VISIBLE else View.GONE
        qualityPanel.visibility = if (name == "quality") View.VISIBLE else View.GONE
        morePanel.visibility = if (name == "more") View.VISIBLE else View.GONE
        volumeHud.visibility = if (name == "volume") View.VISIBLE else View.GONE

        panelFocusables.clear()
        when (name) {
            "sync" -> {
                syncRows.removeAllViews()
                syncEmpty.visibility = View.GONE
                panelFocusables.add(btnResync)
                panelFocusables.add(btnSyncClose)
                startSyncPolling()
            }
            "quality" -> buildQualityRows()
            "more" -> buildMoreRows()
            "volume" -> updateVolumeHud()
        }
        if (panelFocusables.isNotEmpty()) focusPanelItem(panelFocusIndex)
    }

    private fun closePanel() {
        if (panel.isEmpty()) return
        closePanelQuietly()
        showBar(autoHide = player.isPlaying, focusButton = barFocus)
        refreshUi()
    }

    private fun closePanelQuietly() {
        if (panel.isEmpty()) {
            syncPanel.visibility = View.GONE
            qualityPanel.visibility = View.GONE
            morePanel.visibility = View.GONE
            volumeHud.visibility = View.GONE
            return
        }
        panel = ""
        panelFocusables.clear()
        ui.removeCallbacks(syncTick)
        syncPanel.visibility = View.GONE
        qualityPanel.visibility = View.GONE
        morePanel.visibility = View.GONE
        volumeHud.visibility = View.GONE
    }

    private fun focusPanelItem(index: Int) {
        if (panelFocusables.isEmpty()) return
        panelFocusIndex = index.coerceIn(0, panelFocusables.size - 1)
        panelFocusables[panelFocusIndex].requestFocus()
    }

    private fun movePanelFocus(delta: Int) {
        if (panelFocusables.isEmpty()) return
        val current = currentFocus
        if (current != null) {
            val index = panelFocusables.indexOf(current)
            if (index >= 0) panelFocusIndex = index
        }
        focusPanelItem(panelFocusIndex + delta)
    }

    private fun activatePanelFocus() {
        if (panelFocusables.isEmpty()) return
        val view = panelFocusables[panelFocusIndex.coerceIn(0, panelFocusables.size - 1)]
        if (view.isClickable) view.performClick()
    }

    // ---- 清晰度面板 ----

    private fun qualityOptions(): List<Triple<String, String, Int>> = listOf(
        Triple("自动（推荐）", "按带宽自动选", 0),
        Triple("1080p", "最高 1920×1080", 1080),
        Triple("720p", "省带宽，适合无线不稳", 720),
        Triple("480p", "最低带宽", 480),
    )

    private fun buildQualityRows() {
        qualityRows.removeAllViews()
        panelFocusables.clear()
        val options = qualityOptions()
        var focusIndex = 0
        options.forEachIndexed { index, option ->
            val selected = option.third == qualityHeight
            if (selected) focusIndex = index
            val row = makeOptionRow(option.first, option.second, selected) {
                selectQuality(option.third)
            }
            qualityRows.addView(row)
            panelFocusables.add(row)
        }
        qualityNow.text = currentVideoLine()
        panelFocusIndex = focusIndex
    }

    private fun selectQuality(height: Int) {
        qualityHeight = height
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putInt(KEY_QUALITY, height).apply()
        applyQuality()
        val multiple = hasMultipleVideoFormats()
        showToast(if (multiple) "清晰度已切换" else "当前片源只有一种清晰度，已尽力适配")
        closePanel()
    }

    /** 上限交给 ExoPlayer 的 TrackSelector：超出上限的清晰度会被过滤掉，立即对当前片源生效。 */
    private fun applyQuality() {
        try {
            val params = trackSelector.buildUponParameters()
            when (qualityHeight) {
                1080 -> params.setMaxVideoSize(1920, 1080)
                720 -> params.setMaxVideoSize(1280, 720)
                480 -> params.setMaxVideoSize(854, 480)
                else -> params.setMaxVideoSize(Int.MAX_VALUE, Int.MAX_VALUE)
            }
            trackSelector.setParameters(params)
        } catch (e: Exception) {
            Log.w(TAG, "切换清晰度失败: " + e.message)
        }
    }

    private fun hasMultipleVideoFormats(): Boolean {
        return try {
            player.currentTracks.groups.any { group ->
                group.type == C.TRACK_TYPE_VIDEO && group.length > 1
            }
        } catch (e: Exception) {
            false
        }
    }

    /** 面板底部「当前画面：1920×1080 · AVC · 约 2.4 Mbps」。 */
    private fun currentVideoLine(): String {
        val format: Format? = try {
            player.videoFormat
        } catch (e: Exception) {
            null
        }
        if (format == null || format.width <= 0 || format.height <= 0) return "当前画面：--"
        val codec = when (format.sampleMimeType) {
            "video/avc" -> "AVC"
            "video/hevc" -> "HEVC"
            "video/av01" -> "AV1"
            "video/x-vnd.on2.vp9" -> "VP9"
            "video/mp4v-es" -> "MPEG-4"
            else -> ""
        }
        val parts = ArrayList<String>()
        parts.add(format.width.toString() + "×" + format.height)
        if (codec.isNotEmpty()) parts.add(codec)
        if (format.bitrate > 0) {
            parts.add("约 " + String.format(Locale.US, "%.1f", format.bitrate / 1000000.0) + " Mbps")
        }
        return "当前画面：" + parts.joinToString(" · ")
    }

    // ---- 更多面板（画面比例） ----

    private fun moreOptions(): List<Triple<String, String, Int>> = listOf(
        Triple("适应", "保持比例，画面完整", AspectRatioFrameLayout.RESIZE_MODE_FIT),
        Triple("拉伸", "铺满屏幕，可能变形", AspectRatioFrameLayout.RESIZE_MODE_FILL),
        Triple("原始", "裁掉边缘，铺满屏幕", AspectRatioFrameLayout.RESIZE_MODE_ZOOM),
    )

    private fun buildMoreRows() {
        moreRows.removeAllViews()
        panelFocusables.clear()
        val options = moreOptions()
        var focusIndex = 0
        options.forEachIndexed { index, option ->
            val selected = playerView.resizeMode == option.third
            if (selected) focusIndex = index
            val row = makeOptionRow(option.first, option.second, selected) {
                playerView.resizeMode = option.third
                showToast("画面比例已切换")
                closePanel()
            }
            moreRows.addView(row)
            panelFocusables.add(row)
        }
        moreVersion.text = "SameCast " + appVersion + " · 接收端"
        panelFocusIndex = focusIndex
    }

    /** 一行选项：左标题+说明，右单选圈；选中行描边高亮、聚焦行再套一层焦点框。 */
    private fun makeOptionRow(title: String, desc: String, selected: Boolean, onPick: () -> Unit): View {
        val row = LinearLayout(this)
        row.orientation = LinearLayout.HORIZONTAL
        row.gravity = Gravity.CENTER_VERTICAL
        row.setBackgroundResource(R.drawable.bg_option_row)
        row.isFocusable = true
        row.isClickable = true
        row.isSelected = selected
        row.setPadding(dp(16), dp(11), dp(14), dp(11))
        val rowParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        )
        rowParams.bottomMargin = dp(8)
        row.layoutParams = rowParams

        val texts = LinearLayout(this)
        texts.orientation = LinearLayout.VERTICAL
        row.addView(
            texts,
            LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f),
        )

        val name = TextView(this)
        name.text = title
        name.setTextColor(0xFFEAF3FF.toInt())
        name.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        name.typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        texts.addView(
            name,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ),
        )

        val sub = TextView(this)
        sub.text = desc
        sub.setTextColor(0xFF6F86A0.toInt())
        sub.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
        val subParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        )
        subParams.topMargin = dp(3)
        texts.addView(sub, subParams)

        val radio = ImageView(this)
        radio.setImageResource(if (selected) R.drawable.ic_radio_on else R.drawable.ic_radio_off)
        radio.isFocusable = false
        val radioParams = LinearLayout.LayoutParams(dp(18), dp(18))
        radioParams.marginStart = dp(12)
        row.addView(radio, radioParams)

        row.setOnClickListener { onPick() }
        return row
    }

    // ---- 同步面板 ----

    private fun startSyncPolling() {
        ui.removeCallbacks(syncTick)
        ui.post(syncTick)
    }

    private fun fetchSyncStatus() {
        if (syncFetching) return
        syncFetching = true
        client.fetchStatus { result ->
            ui.post {
                syncFetching = false
                if (panel == "sync") {
                    renderSync(result)
                } else {
                    updateGroupStats(result)
                    if (barVisible) refreshControlBar()
                }
            }
        }
    }

    /** 控制栏打开时每 3 秒刷一次整组信息，避免顶部台数一直停在 1。 */
    private fun maybeRefreshGroupStatus() {
        val now = System.currentTimeMillis()
        if (now - lastStatusFetch < 3000) return
        lastStatusFetch = now
        fetchSyncStatus()
    }

    /** 用网关的整组快照渲染每台电视：偏差 = 本机进度 - 整组进度（毫秒）。 */
    private fun renderSync(snapshot: JSONObject?) {
        syncRows.removeAllViews()
        if (snapshot == null) {
            syncEmpty.visibility = View.VISIBLE
            if (barVisible) refreshControlBar()
            return
        }
        syncEmpty.visibility = View.GONE
        updateGroupStats(snapshot)
        val groupPosition = snapshot.optDouble("position", 0.0)
        val receivers = snapshot.optJSONArray("receivers")
        if (receivers != null) {
            for (i in 0 until receivers.length()) {
                val receiver = receivers.optJSONObject(i) ?: continue
                val name = receiver.optString("name").ifEmpty { "电视 " + (i + 1) }
                val deviation = Math.round(
                    (receiver.optDouble("position", 0.0) - groupPosition) * 1000.0,
                ).toInt()
                addSyncRow(name, deviation)
            }
        }
        if (barVisible) refreshControlBar()
    }

    /** 从整组快照里取台数和最大偏差；顶部「N 台电视同步」和「已同步/校准中」都靠它。 */
    private fun updateGroupStats(snapshot: JSONObject?) {
        if (snapshot == null) return
        val groupPosition = snapshot.optDouble("position", 0.0)
        val receivers = snapshot.optJSONArray("receivers") ?: return
        castSource = snapshot.optString("source")
        if (receivers.length() > 0) tvCount = receivers.length()
        var worst = 0
        for (i in 0 until receivers.length()) {
            val receiver = receivers.optJSONObject(i) ?: continue
            val deviation = Math.round(
                (receiver.optDouble("position", 0.0) - groupPosition) * 1000.0,
            ).toInt()
            if (Math.abs(deviation) > worst) worst = Math.abs(deviation)
        }
        maxDeviationMs = worst
    }

    private fun addSyncRow(name: String, deviationMs: Int) {
        val row = layoutInflater.inflate(R.layout.item_sync_row, syncRows, false)
        row.findViewById<TextView>(R.id.sync_row_name).text = name
        val aligned = Math.abs(deviationMs) < DEVIATION_OK_MS
        val color = if (aligned) 0xFF4ADE80.toInt() else 0xFFFBBF24.toInt()
        val deviation = row.findViewById<TextView>(R.id.sync_row_dev)
        deviation.text = formatDeviation(deviationMs)
        deviation.setTextColor(color)
        row.findViewById<TextView>(R.id.sync_row_check).setTextColor(color)
        syncRows.addView(row)
        val params = row.layoutParams as LinearLayout.LayoutParams
        params.bottomMargin = dp(8)
        row.layoutParams = params
    }

    private fun formatDeviation(ms: Int): String {
        if (ms == 0) return "0 ms"
        val sign = if (ms > 0) "+" else "-"
        return sign + Math.abs(ms) + " ms"
    }

    /** 「立即对齐」：让网关把整组拉回同一时刻，然后立刻刷新一遍偏差。 */
    private fun resyncNow() {
        client.control("resync") { _, ok ->
            ui.post {
                if (ok) {
                    showToast("已重新对齐所有电视")
                    maxDeviationMs = 0
                    fetchSyncStatus()
                } else {
                    showToast("对齐失败，请检查网关")
                }
                if (barVisible) refreshControlBar()
            }
        }
    }

    // ---- 音量 ----

    private fun volumePercent(): Int {
        val max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        if (max <= 0) return 0
        return audioManager.getStreamVolume(AudioManager.STREAM_MUSIC) * 100 / max
    }

    private fun updateVolumeHud() {
        volumeHud.text = "音量 " + volumePercent() + "% · 左右调节"
    }

    /** 音量键 / 音量面板：调系统媒体音量，并给一条轻提示。 */
    private fun adjustVolume(delta: Int) {
        val max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        val current = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)
        val next = (current + delta).coerceIn(0, max)
        if (next != current) {
            try {
                audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, next, 0)
            } catch (e: Exception) {
                Log.w(TAG, "调整音量失败: " + e.message)
            }
        }
        if (panel == "volume") {
            updateVolumeHud()
        } else {
            volumeHud.text = "音量 " + volumePercent() + "%"
            volumeHud.visibility = View.VISIBLE
            ui.removeCallbacks(hideVolumeRunnable)
            ui.postDelayed(hideVolumeRunnable, VOLUME_TIMEOUT)
        }
    }

    // ---- 轻提示 ----

    private fun showToast(text: String) {
        if (!::toastView.isInitialized) return
        toastQueue.add(text)
        if (!toastShowing) nextToast()
    }

    private fun nextToast() {
        if (toastQueue.isEmpty()) {
            toastShowing = false
            toastView.visibility = View.GONE
            return
        }
        val text = toastQueue.removeAt(0)
        toastShowing = true
        toastView.text = text
        toastView.visibility = View.VISIBLE
        ui.removeCallbacks(hideToastRunnable)
        ui.postDelayed(hideToastRunnable, TOAST_TIMEOUT)
    }

    private fun finishToast() {
        toastShowing = false
        nextToast()
    }

    // ---- 遥控器 ----

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_UP) {
            adjustVolume(1)
            return true
        }
        if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            adjustVolume(-1)
            return true
        }
        if (panel.isNotEmpty()) return handlePanelKey(keyCode)
        if (barVisible) return handleBarKey(keyCode)
        return handleHiddenKey(keyCode)
    }

    /** 面板打开：上下换选项，确定执行，返回关闭。 */
    private fun handlePanelKey(keyCode: Int): Boolean {
        if (panel == "volume") {
            return when (keyCode) {
                KeyEvent.KEYCODE_DPAD_LEFT -> {
                    adjustVolume(-1)
                    true
                }
                KeyEvent.KEYCODE_DPAD_RIGHT -> {
                    adjustVolume(1)
                    true
                }
                KeyEvent.KEYCODE_BACK,
                KeyEvent.KEYCODE_DPAD_CENTER,
                KeyEvent.KEYCODE_ENTER,
                KeyEvent.KEYCODE_NUMPAD_ENTER,
                KeyEvent.KEYCODE_SPACE -> {
                    closePanel()
                    true
                }
                else -> false
            }
        }
        when (keyCode) {
            KeyEvent.KEYCODE_BACK -> {
                closePanel()
                return true
            }
            KeyEvent.KEYCODE_DPAD_UP -> {
                movePanelFocus(-1)
                return true
            }
            KeyEvent.KEYCODE_DPAD_DOWN -> {
                movePanelFocus(1)
                return true
            }
            KeyEvent.KEYCODE_DPAD_CENTER,
            KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_NUMPAD_ENTER,
            KeyEvent.KEYCODE_SPACE -> {
                activatePanelFocus()
                return true
            }
        }
        return false
    }

    /**
     * 控制栏（下方菜单）可见时：左右键始终用来切换按钮，不再快进/快退，确定执行，
     * 下键或返回收起菜单。收起后全屏时，左右键才是快退/快进 10 秒。
     * 遥控器上独立的快退/快进键（MEDIA_REWIND / MEDIA_FAST_FORWARD）任何时候都能用。
     */
    private fun handleBarKey(keyCode: Int): Boolean {
        if (barFocus < 0) setBarFocus(BUTTON_PLAY)
        return when (keyCode) {
            KeyEvent.KEYCODE_DPAD_LEFT -> {
                moveBarFocus(-1)
                true
            }
            KeyEvent.KEYCODE_DPAD_RIGHT -> {
                moveBarFocus(1)
                true
            }
            KeyEvent.KEYCODE_DPAD_UP -> {
                setBarFocus(BUTTON_PLAY)
                resetBarTimer()
                true
            }
            KeyEvent.KEYCODE_DPAD_DOWN,
            KeyEvent.KEYCODE_BACK -> {
                hideBarNow()
                true
            }
            KeyEvent.KEYCODE_DPAD_CENTER,
            KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_NUMPAD_ENTER,
            KeyEvent.KEYCODE_SPACE -> {
                activateBarButton(barFocus)
                true
            }
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_MEDIA_PLAY,
            KeyEvent.KEYCODE_MEDIA_PAUSE -> {
                togglePlayback()
                true
            }
            KeyEvent.KEYCODE_MEDIA_REWIND -> {
                seekBy(-SEEK_STEP)
                true
            }
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> {
                seekBy(SEEK_STEP)
                true
            }
            KeyEvent.KEYCODE_INFO,
            KeyEvent.KEYCODE_MENU -> {
                resetBarTimer()
                true
            }
            else -> false
        }
    }

    /** 控制栏收起时的按键：确定播放/暂停并呼出控制栏，上呼出并聚焦播放，左右快进/快退。 */
    private fun handleHiddenKey(keyCode: Int): Boolean {
        if (player.mediaItemCount == 0) return false
        return when (keyCode) {
            KeyEvent.KEYCODE_DPAD_CENTER,
            KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_NUMPAD_ENTER,
            KeyEvent.KEYCODE_SPACE,
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_MEDIA_PLAY,
            KeyEvent.KEYCODE_MEDIA_PAUSE -> {
                togglePlayback()
                showBar(autoHide = player.isPlaying)
                true
            }
            KeyEvent.KEYCODE_DPAD_LEFT,
            KeyEvent.KEYCODE_MEDIA_REWIND -> {
                seekBy(-SEEK_STEP)
                true
            }
            KeyEvent.KEYCODE_DPAD_RIGHT,
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> {
                seekBy(SEEK_STEP)
                true
            }
            KeyEvent.KEYCODE_DPAD_UP -> {
                showBar(autoHide = player.isPlaying, focusButton = BUTTON_PLAY)
                true
            }
            KeyEvent.KEYCODE_DPAD_DOWN,
            KeyEvent.KEYCODE_INFO,
            KeyEvent.KEYCODE_MENU -> {
                showBar(autoHide = player.isPlaying)
                true
            }
            else -> false
        }
    }

    /** 遥控器播放/暂停：本机切换并上报网关，由网关广播给其他电视。 */
    private fun togglePlayback() {
        if (player.mediaItemCount == 0) return
        if (errorText != null) {
            errorText = null
            player.prepare()
            showToast("正在重试…")
            refreshUi()
            return
        }
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
        refreshControlBar()
        resetBarTimer(player.isPlaying)
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
        showToast(if (delta > 0) "快进 " + delta.toInt() + " 秒" else "快退 " + (-delta).toInt() + " 秒")
        client.sendCommand(JSONObject().put("cmd", "seek").put("position", target / 1000.0))
        report(true)
        refreshControlBar()
        refreshProgress()
        resetBarTimer(player.isPlaying)
    }

    /** 返回键：有面板先关面板，控制栏开着先收起，否则退回电视主页（进程继续在线）。 */
    override fun onBackPressed() {
        when {
            panel.isNotEmpty() -> closePanel()
            barVisible && player.isPlaying -> hideBarNow()
            else -> moveTaskToBack(true)
        }
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
                    closePanelQuietly()
                    reclaimScreen()
                    errorText = null
                    mediaTitle = titleFromMetadata(cmd.optString("metadata")).ifEmpty { guessTitle(uri) }
                    player.setMediaItem(MediaItem.fromUri(uri))
                    player.prepare()
                    ensureForeground()
                    showToast("手机开始投屏")
                    showBar(autoHide = false)
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
                refreshProgress()
            }
            "stop" -> {
                closePanelQuietly()
                hideBarNow()
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

    /** 网关连接状态：待机首页上的一行小字 + 状态灯；播放中还会影响顶部「已同步/未连接网关」。 */
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
        val hasMedia = ::player.isInitialized && player.mediaItemCount > 0
        when {
            connected && !gatewayOnline -> showToast("已连接 SameCast 网关")
            !connected && gatewayOnline && hasMedia -> showToast("与网关的连接中断，正在重连…")
        }
        gatewayOnline = connected
        if (barVisible) refreshControlBar()
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
        val match = TITLE_RE.find(metadata) ?: return ""
        return match.groupValues[1].trim()
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

    /**
     * 收到播放指令时把接收端拉回前台：电视自带投屏界面还开着的话会盖住画面。
     */
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
        closePanelQuietly()
        hideBarNow()
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

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density + 0.5f).toInt()

    override fun onDestroy() {
        ui.removeCallbacksAndMessages(null)
        abandonAudioFocus()
        client.stop()
        player.release()
        super.onDestroy()
    }
}
