package io.github.kpganon.termuxmpvcontrols

import android.Manifest
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.GravityCompat
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.MediaController
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionResult
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.google.common.util.concurrent.MoreExecutors
import io.github.kpganon.termuxmpvcontrols.databinding.ActivityMainBinding
import java.io.File

/**
 * Drawer over four views: Now Playing, Current Playlist, Favorites and Settings, with a
 * mini-player pinned to the bottom.
 *
 * Plain views and ViewBinding rather than Compose: the UI is lists and a hero image, and this
 * project runs on AGP 9's built-in Kotlin where adding a compiler plugin is a known rabbit hole.
 */
@UnstableApi
class MainActivity : AppCompatActivity() {

    private enum class Screen { NOW_PLAYING, PLAYLIST, FAVORITES, SETTINGS }

    private lateinit var views: ActivityMainBinding
    private lateinit var settings: Settings
    private lateinit var favorites: FavoritesStore

    private var controller: MediaController? = null
    private var controllerFuture: ListenableFuture<MediaController>? = null

    private val ticker = Handler(Looper.getMainLooper())
    private var screen = Screen.NOW_PLAYING

    /** Playlist rows actually shown, mapped back to their real index in mpv's playlist. */
    private val playlistRows = mutableListOf<Row>()
    private val favoriteRows = mutableListOf<FavoritesStore.Favorite>()

    private var timelineSize = -1
    private var currentIndex = -1
    private var artworkFingerprint = 0
    private var scrubbing = false
    private var hasScrolledToCurrent = false

    private data class Row(val mediaIndex: Int, val title: String, val current: Boolean)

    private lateinit var playlistAdapter: PlaylistAdapter
    private lateinit var favoritesAdapter: FavoritesAdapter

    private val requestNotifications =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) toast(getString(R.string.notifications_denied))
        }

    private val exportFavorites =
        registerForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { uri ->
            uri?.let(::writeFavoritesTo)
        }

    private val importFavorites =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            uri?.let(::readFavoritesFrom)
        }

    private val playerListener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) {
            val timelineChanged = events.contains(Player.EVENT_TIMELINE_CHANGED) ||
                timelineSize != player.mediaItemCount
            render(player, rebuildList = timelineChanged)
        }
    }

    private val controllerListener = object : MediaController.Listener {
        override fun onCustomCommand(
            controller: MediaController,
            command: SessionCommand,
            args: Bundle,
        ): ListenableFuture<SessionResult> {
            if (command.customAction == MpvSessionService.Commands.REFRESH_STATUS) {
                showRefresh(args)
            }
            return Futures.immediateFuture(SessionResult(SessionResult.RESULT_SUCCESS))
        }
    }

    // -- lifecycle -------------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        views = ActivityMainBinding.inflate(layoutInflater)
        setContentView(views.root)

        settings = Settings(this)
        favorites = FavoritesStore(this)

        wireDrawer()
        wireNowPlaying()
        wireLists()
        wireMiniPlayer()
        wireSettings()
        show(Screen.NOW_PLAYING)

        requestNotificationPermissionIfNeeded()
        startService(Intent(this, MpvSessionService::class.java))
    }

    override fun onStart() {
        super.onStart()
        val token = SessionToken(this, ComponentName(this, MpvSessionService::class.java))
        val future = MediaController.Builder(this, token)
            .setListener(controllerListener)
            .buildAsync()
        controllerFuture = future
        future.addListener({
            val ready = runCatching { future.get() }.getOrNull() ?: return@addListener
            controller = ready
            ready.addListener(playerListener)
            render(ready, rebuildList = true)
        }, MoreExecutors.directExecutor())
    }

    override fun onResume() {
        super.onResume()
        scheduleTick()
    }

    override fun onPause() {
        ticker.removeCallbacksAndMessages(null)
        super.onPause()
    }

    override fun onStop() {
        controller?.removeListener(playerListener)
        controllerFuture?.let(MediaController::releaseFuture)
        controllerFuture = null
        controller = null
        super.onStop()
    }

    override fun onBackPressed() {
        if (views.drawerLayout.isDrawerOpen(GravityCompat.START)) {
            views.drawerLayout.closeDrawer(GravityCompat.START)
        } else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }

    // -- wiring ----------------------------------------------------------------------------

    private fun wireDrawer() {
        views.toolbar.setNavigationOnClickListener {
            views.drawerLayout.openDrawer(GravityCompat.START)
        }
        views.navigationView.setNavigationItemSelectedListener { item ->
            when (item.itemId) {
                R.id.nav_now_playing -> show(Screen.NOW_PLAYING)
                R.id.nav_playlist -> show(Screen.PLAYLIST)
                R.id.nav_favorites -> show(Screen.FAVORITES)
                R.id.nav_settings -> show(Screen.SETTINGS)
            }
            item.isChecked = true
            views.drawerLayout.closeDrawer(GravityCompat.START)
            true
        }
    }

    private fun wireNowPlaying() = with(views.nowPlayingView) {
        heroPrevious.setOnClickListener { controller?.seekToPreviousMediaItem() }
        heroNext.setOnClickListener { controller?.seekToNextMediaItem() }
        heroPlayPause.setOnClickListener { togglePlayback() }
        heroShuffle.setOnClickListener { sendCustom(MpvSessionService.Commands.SHUFFLE) }
        heroFavorite.setOnClickListener { toggleFavorite() }

        seekBar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(bar: SeekBar, progress: Int, fromUser: Boolean) {
                if (fromUser) positionLabel.text = formatTime(progress.toLong())
            }

            override fun onStartTrackingTouch(bar: SeekBar) {
                scrubbing = true
            }

            override fun onStopTrackingTouch(bar: SeekBar) {
                scrubbing = false
                controller?.seekTo(bar.progress.toLong())
            }
        })
    }

    private fun wireLists() {
        playlistAdapter = PlaylistAdapter()
        favoritesAdapter = FavoritesAdapter()

        with(views.playlistView) {
            listView.adapter = playlistAdapter
            listView.setOnItemClickListener { _, _, position, _ ->
                playlistRows.getOrNull(position)?.let { controller?.seekTo(it.mediaIndex, 0L) }
            }
            listShuffle.setOnClickListener { sendCustom(MpvSessionService.Commands.SHUFFLE) }
            listRefresh.setOnClickListener {
                listProgress.visibility = View.VISIBLE
                sendCustom(MpvSessionService.Commands.REFRESH_PLAYLIST)
            }
        }

        with(views.favoritesView) {
            listView.adapter = favoritesAdapter
            listView.setOnItemClickListener { _, _, position, _ ->
                favoriteRows.getOrNull(position)?.let { playFavorite(it) }
            }
            listView.setOnItemLongClickListener { _, _, position, _ ->
                favoriteRows.getOrNull(position)?.let {
                    favorites.remove(it.url)
                    toast(getString(R.string.removed_favorite))
                    refreshFavorites()
                }
                true
            }
            // Favorites are a local list; shuffling and refreshing belong to mpv's playlist.
            listShuffle.visibility = View.GONE
            listRefresh.visibility = View.GONE
        }
    }

    private fun wireMiniPlayer() {
        views.miniPrevious.setOnClickListener { controller?.seekToPreviousMediaItem() }
        views.miniNext.setOnClickListener { controller?.seekToNextMediaItem() }
        views.miniPlayPause.setOnClickListener { togglePlayback() }
        views.miniPlayer.setOnClickListener { show(Screen.NOW_PLAYING) }
    }

    private fun wireSettings() = with(views.settingsView) {
        settingHost.setText(settings.host)
        settingPort.setText(settings.port.toString())
        settingToken.setText(settings.token.orEmpty())
        settingLaunchCommand.setText(settings.launchCommand)
        settingHideUnavailable.isChecked = settings.hideUnavailable
        settingRefreshOnLaunch.isChecked = settings.refreshOnLaunch
        settingKeepAlive.isChecked = settings.keepAliveEnabled

        settingHideUnavailable.setOnCheckedChangeListener { _, checked ->
            settings.hideUnavailable = checked
            controller?.let { render(it, rebuildList = true) }
        }
        settingRefreshOnLaunch.setOnCheckedChangeListener { _, checked ->
            settings.refreshOnLaunch = checked
        }
        settingKeepAlive.setOnCheckedChangeListener { _, checked ->
            settings.keepAliveEnabled = checked
            // Takes effect on the next state push from the daemon.
            toast(if (checked) "Keep-alive on" else "Keep-alive off — headset buttons disabled")
        }

        settingStart.setOnClickListener { startInTermux() }
        settingExport.setOnClickListener { exportFavorites.launch(FavoritesStore.EXPORT_NAME) }
        settingImport.setOnClickListener { importFavorites.launch(arrayOf("application/json")) }

        aboutText.text = buildString {
            append("App ").append(BuildConfig.VERSION_NAME)
            append("\nProtocol v").append(Protocol.VERSION)
            append("\nBridge ").append(settings.host).append(':').append(settings.port)
            append("\nTermux ")
            append(if (TermuxLauncher.isInstalled(this@MainActivity)) "installed" else "not found")
        }
        favoritesStatus.text = getString(R.string.favorites_subtitle, favorites.all().size)
    }

    private fun persistConnectionSettings() = with(views.settingsView) {
        settings.host = settingHost.text.toString().trim().ifEmpty { Settings.DEFAULT_HOST }
        settings.port = settingPort.text.toString().trim().toIntOrNull() ?: Settings.DEFAULT_PORT
        settings.token = settingToken.text.toString().trim()
        settings.launchCommand = settingLaunchCommand.text.toString().trim()
            .ifEmpty { Settings.DEFAULT_LAUNCH_COMMAND }
    }

    // -- actions ---------------------------------------------------------------------------

    private fun togglePlayback() {
        val active = controller ?: return
        if (active.playWhenReady) active.pause() else active.play()
    }

    private fun sendCustom(action: String, args: Bundle = Bundle.EMPTY) {
        val active = controller ?: return
        active.sendCustomCommand(SessionCommand(action, Bundle.EMPTY), args)
        if (action == MpvSessionService.Commands.SHUFFLE) toast(getString(R.string.shuffled))
    }

    private fun currentUrl(): String? =
        controller?.mediaMetadata?.extras?.getString(MpvPlayer.EXTRA_URL)

    private fun toggleFavorite() {
        val active = controller ?: return
        val url = currentUrl()
        if (url.isNullOrEmpty()) {
            toast("No track URL to save")
            return
        }
        val added = favorites.toggle(
            FavoritesStore.Favorite(
                url = url,
                title = active.mediaMetadata.title?.toString() ?: url,
                artist = active.mediaMetadata.artist?.toString(),
            )
        )
        toast(getString(if (added) R.string.added_favorite else R.string.removed_favorite))
        renderFavoriteIcon()
        refreshFavorites()
    }

    private fun playFavorite(favorite: FavoritesStore.Favorite) {
        sendCustom(
            MpvSessionService.Commands.PLAY_URL,
            Bundle().apply { putString("url", favorite.url) },
        )
        toast(favorite.title)
        show(Screen.NOW_PLAYING)
    }

    private fun startInTermux() {
        persistConnectionSettings()
        val message = when (val result = TermuxLauncher.run(this, settings.launchCommand)) {
            TermuxLauncher.Result.Started -> getString(R.string.launch_started)
            TermuxLauncher.Result.TermuxMissing -> getString(R.string.launch_no_termux)
            TermuxLauncher.Result.PermissionMissing -> getString(R.string.launch_no_permission)
            is TermuxLauncher.Result.Failed -> getString(R.string.launch_failed, result.reason)
        }
        views.settingsView.launchStatus.text = message
        toast(message)
    }

    private fun writeFavoritesTo(uri: Uri) {
        val staged = File(cacheDir, FavoritesStore.EXPORT_NAME)
        val count = favorites.exportTo(staged)
        contentResolver.openOutputStream(uri)?.use { out ->
            staged.inputStream().use { it.copyTo(out) }
        }
        toast(getString(R.string.exported_favorites, count))
    }

    private fun readFavoritesFrom(uri: Uri) {
        val staged = File(cacheDir, "import.json")
        contentResolver.openInputStream(uri)?.use { input ->
            staged.outputStream().use { input.copyTo(it) }
        } ?: run {
            toast(getString(R.string.import_missing))
            return
        }
        val added = runCatching { favorites.importFrom(staged) }.getOrElse { 0 }
        toast(getString(R.string.imported_favorites, added))
        refreshFavorites()
    }

    // -- rendering -------------------------------------------------------------------------

    private fun show(target: Screen) {
        screen = target
        views.nowPlayingView.root.visibility = visibleIf(target == Screen.NOW_PLAYING)
        views.playlistView.root.visibility = visibleIf(target == Screen.PLAYLIST)
        views.favoritesView.root.visibility = visibleIf(target == Screen.FAVORITES)
        views.settingsView.root.visibility = visibleIf(target == Screen.SETTINGS)

        views.toolbar.title = getString(
            when (target) {
                Screen.NOW_PLAYING -> R.string.nav_now_playing
                Screen.PLAYLIST -> R.string.nav_playlist
                Screen.FAVORITES -> R.string.nav_favorites
                Screen.SETTINGS -> R.string.nav_settings
            }
        )
        if (target == Screen.FAVORITES) refreshFavorites()
        if (target != Screen.SETTINGS) persistConnectionSettings()
    }

    private fun visibleIf(condition: Boolean) = if (condition) View.VISIBLE else View.GONE

    private fun render(player: Player, rebuildList: Boolean) {
        val count = player.mediaItemCount
        val index = player.currentMediaItemIndex
        val metadata = player.mediaMetadata
        val title = metadata.title?.toString()?.takeIf { it.isNotBlank() }
            ?: getString(R.string.nothing_playing)
        val artist = metadata.artist?.toString()?.takeIf { it.isNotBlank() }

        views.nowPlayingView.heroTitle.text = title
        views.nowPlayingView.heroArtist.text = artist.orEmpty()
        views.nowPlayingView.heroArtist.visibility = visibleIf(artist != null)
        views.miniTitle.text = title

        val status = if (count == 0) {
            getString(R.string.waiting_for_mpv)
        } else {
            val state = getString(if (player.playWhenReady) R.string.playing else R.string.paused)
            getString(R.string.playlist_status, state, index + 1, count)
        }
        views.miniStatus.text = status
        drawerStatusView()?.text = status

        val playIcon = if (player.playWhenReady) R.drawable.ic_pause else R.drawable.ic_play
        views.nowPlayingView.heroPlayPause.setImageResource(playIcon)
        views.miniPlayPause.setImageResource(playIcon)

        renderArtwork(metadata.artworkData)
        renderFavoriteIcon()
        renderSeekBar(player)

        if (rebuildList) {
            rebuildPlaylist(player)
            timelineSize = count
            hasScrolledToCurrent = false
        }
        if (rebuildList || index != currentIndex) {
            currentIndex = index
            rebuildPlaylist(player)
            playlistAdapter.notifyDataSetChanged()
        }

        if (!hasScrolledToCurrent && playlistRows.isNotEmpty()) {
            val row = playlistRows.indexOfFirst { it.current }
            if (row >= 0) views.playlistView.listView.setSelection(row)
            hasScrolledToCurrent = true
        }
    }

    private fun rebuildPlaylist(player: Player) {
        playlistRows.clear()
        val hide = settings.hideUnavailable
        var hidden = 0
        for (i in 0 until player.mediaItemCount) {
            val item: MediaItem = player.getMediaItemAt(i)
            val extras = item.mediaMetadata.extras
            val unavailable = extras?.getBoolean(MpvPlayer.EXTRA_UNAVAILABLE) ?: false
            val isCurrent = i == player.currentMediaItemIndex
            // Never hide what is actually playing, however broken its metadata looks.
            if (hide && unavailable && !isCurrent) {
                hidden++
                continue
            }
            playlistRows.add(
                Row(
                    mediaIndex = i,
                    title = item.mediaMetadata.title?.toString().orEmpty(),
                    current = isCurrent,
                )
            )
        }
        views.playlistView.listSubtitle.text = when {
            playlistRows.isEmpty() -> ""
            hidden > 0 -> getString(R.string.playlist_subtitle_hidden, playlistRows.size, hidden)
            else -> getString(R.string.playlist_subtitle, playlistRows.size)
        }
        views.playlistView.listEmpty.visibility = visibleIf(playlistRows.isEmpty())
        views.playlistView.listEmpty.text = getString(R.string.playlist_empty)
    }

    private fun refreshFavorites() {
        favoriteRows.clear()
        favoriteRows.addAll(favorites.all())
        favoritesAdapter.notifyDataSetChanged()
        views.favoritesView.listSubtitle.text =
            getString(R.string.favorites_subtitle, favoriteRows.size)
        views.favoritesView.listEmpty.visibility = visibleIf(favoriteRows.isEmpty())
        views.favoritesView.listEmpty.text = getString(R.string.favorites_empty)
        views.settingsView.favoritesStatus.text =
            getString(R.string.favorites_subtitle, favoriteRows.size)
    }

    private fun renderFavoriteIcon() {
        val starred = favorites.contains(currentUrl())
        views.nowPlayingView.heroFavorite.setImageResource(
            if (starred) R.drawable.ic_star_filled else R.drawable.ic_star
        )
    }

    private fun renderSeekBar(player: Player) {
        if (scrubbing) return
        val duration = player.duration.takeIf { it > 0 } ?: 0L
        val position = player.currentPosition.coerceAtLeast(0L)
        views.nowPlayingView.seekBar.max = duration.toInt()
        views.nowPlayingView.seekBar.progress = position.coerceAtMost(duration).toInt()
        views.nowPlayingView.positionLabel.text = formatTime(position)
        views.nowPlayingView.durationLabel.text = formatTime(duration)
    }

    private fun renderArtwork(data: ByteArray?) {
        // contentHashCode is cheap next to decoding a JPEG on every playback event.
        val fingerprint = data?.contentHashCode() ?: 0
        if (fingerprint == artworkFingerprint) return
        artworkFingerprint = fingerprint

        val bitmap = data?.let {
            runCatching { BitmapFactory.decodeByteArray(it, 0, it.size) }.getOrNull()
        }
        val heroPadding = (60 * resources.displayMetrics.density).toInt()
        val miniPadding = (8 * resources.displayMetrics.density).toInt()
        if (bitmap == null) {
            views.nowPlayingView.heroArtwork.setImageResource(R.drawable.ic_album_placeholder)
            views.nowPlayingView.heroArtwork.setPadding(heroPadding, heroPadding, heroPadding, heroPadding)
            views.miniArtwork.setImageResource(R.drawable.ic_album_placeholder)
            views.miniArtwork.setPadding(miniPadding, miniPadding, miniPadding, miniPadding)
        } else {
            views.nowPlayingView.heroArtwork.setImageBitmap(bitmap)
            views.nowPlayingView.heroArtwork.setPadding(0, 0, 0, 0)
            views.miniArtwork.setImageBitmap(bitmap)
            views.miniArtwork.setPadding(0, 0, 0, 0)
        }
    }

    private fun showRefresh(args: Bundle) {
        val status = args.getString("status").orEmpty()
        views.playlistView.listProgress.visibility = visibleIf(status == "running")
        val message = when (status) {
            "running" -> getString(R.string.refresh_running)
            "done" -> {
                val added = args.getInt("added")
                if (added > 0) getString(R.string.refresh_added, added)
                else getString(R.string.refresh_up_to_date)
            }

            else -> getString(R.string.refresh_failed, args.getString("reason") ?: status)
        }
        toast(message)
    }

    /** Media3 does not push position updates, so the seek bar is driven from here while visible. */
    private fun scheduleTick() {
        ticker.removeCallbacksAndMessages(null)
        ticker.postDelayed(object : Runnable {
            override fun run() {
                controller?.let { if (screen == Screen.NOW_PLAYING) renderSeekBar(it) }
                ticker.postDelayed(this, TICK_MS)
            }
        }, TICK_MS)
    }

    private fun drawerStatusView(): TextView? =
        views.navigationView.getHeaderView(0)?.findViewById(R.id.drawerStatus)

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < 33) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun toast(message: String) =
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()

    private fun formatTime(ms: Long): String {
        val total = ms / 1000
        val minutes = total / 60
        val seconds = total % 60
        return if (minutes >= 60) {
            String.format("%d:%02d:%02d", minutes / 60, minutes % 60, seconds)
        } else {
            String.format("%d:%02d", minutes, seconds)
        }
    }

    // -- adapters --------------------------------------------------------------------------

    private abstract inner class RowAdapter : BaseAdapter() {
        override fun getItemId(position: Int): Long = position.toLong()

        protected fun bind(
            convertView: View?,
            parent: ViewGroup,
            index: String,
            title: String,
            highlight: Boolean,
        ): View {
            val view = convertView ?: LayoutInflater.from(parent.context)
                .inflate(R.layout.row_playlist, parent, false)
            val indexView = view.findViewById<TextView>(R.id.index)
            val titleView = view.findViewById<TextView>(R.id.title)
            val colour = ContextCompat.getColor(
                this@MainActivity,
                if (highlight) R.color.accent else R.color.text_primary
            )
            indexView.text = index
            indexView.setTextColor(
                ContextCompat.getColor(
                    this@MainActivity,
                    if (highlight) R.color.accent else R.color.text_tertiary
                )
            )
            titleView.text = title
            titleView.setTextColor(colour)
            view.setBackgroundResource(if (highlight) R.drawable.bg_row_current else 0)
            return view
        }
    }

    private inner class PlaylistAdapter : RowAdapter() {
        override fun getCount(): Int = playlistRows.size
        override fun getItem(position: Int): Row = playlistRows[position]

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val row = playlistRows[position]
            // Numbered by mpv's own index, so what you see matches what mpv reports.
            val label = if (row.current) "▶" else (row.mediaIndex + 1).toString()
            return bind(convertView, parent, label, row.title, row.current)
        }
    }

    private inner class FavoritesAdapter : RowAdapter() {
        override fun getCount(): Int = favoriteRows.size
        override fun getItem(position: Int): FavoritesStore.Favorite = favoriteRows[position]

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val favorite = favoriteRows[position]
            val label = favorite.artist?.takeIf { it.isNotBlank() }
                ?.let { "${favorite.title}  ·  $it" } ?: favorite.title
            return bind(convertView, parent, "★", label, false)
        }
    }

    private companion object {
        const val TICK_MS = 500L
    }
}
