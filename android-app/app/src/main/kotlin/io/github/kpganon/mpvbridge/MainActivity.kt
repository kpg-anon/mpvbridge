package io.github.kpganon.mpvbridge

import android.Manifest
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.EditText
import android.widget.ImageView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.widget.doAfterTextChanged
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
import io.github.kpganon.mpvbridge.databinding.ActivityMainBinding
import java.io.File

/**
 * Drawer over five views: Now Playing, Library, Current Playlist, Favorites and Settings, with a
 * mini-player pinned to the bottom.
 *
 * Plain views and ViewBinding rather than Compose: the UI is lists and a hero image, and this
 * project runs on AGP 9's built-in Kotlin where adding a compiler plugin is a known rabbit hole.
 */
@UnstableApi
class MainActivity : AppCompatActivity() {

    private enum class Screen { NOW_PLAYING, LIBRARY, PLAYLIST, FAVORITES, SETTINGS }

    private lateinit var views: ActivityMainBinding
    private lateinit var settings: Settings
    private lateinit var favorites: FavoritesStore

    private var controller: MediaController? = null
    private var controllerFuture: ListenableFuture<MediaController>? = null

    private val ticker = Handler(Looper.getMainLooper())
    private val autoStartTimer = Handler(Looper.getMainLooper())
    private var screen = Screen.NOW_PLAYING

    /** Last thing the service told us about the daemon; null until it has told us anything. */
    private var bridgeConnected: Boolean? = null

    /** One Termux launch per visit, however many times the status flaps. */
    private var autoStartDone = false

    /** Set while the Termux permission dialog is up, so a grant continues what asked for it. */
    private var launchAfterPermission = false

    /** Playlist rows actually shown, mapped back to their real index in mpv's playlist. */
    private val playlistRows = mutableListOf<Row>()
    private val favoriteRows = mutableListOf<FavoritesStore.Favorite>()
    private val libraryRows = mutableListOf<Protocol.SavedPlaylist>()

    /** Every playlist row, before the filter box narrows it down. */
    private val allPlaylistRows = mutableListOf<Row>()

    private var hiddenUnavailable = 0
    private var timelineSize = -1
    private var currentIndex = -1
    private var artworkFingerprint = 0
    private var scrubbing = false
    private var hasScrolledToCurrent = false

    private data class Row(val mediaIndex: Int, val title: String, val current: Boolean)

    private lateinit var playlistAdapter: PlaylistAdapter
    private lateinit var favoritesAdapter: FavoritesAdapter
    private lateinit var libraryAdapter: LibraryAdapter

    private val requestNotifications =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (!granted) toast(getString(R.string.notifications_denied))
        }

    /**
     * `com.termux.permission.RUN_COMMAND` is declared by Termux as a *dangerous* permission, so
     * it is never granted at install time however plainly this app requests it in its manifest.
     * Without this the Start button and auto-start both fail silently.
     */
    private val requestTermuxPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val wanted = launchAfterPermission
            launchAfterPermission = false
            if (!granted) {
                views.settingsView.launchStatus.text = getString(R.string.launch_no_permission)
                toast(getString(R.string.launch_no_permission))
                return@registerForActivityResult
            }
            if (wanted) startInTermux()
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
            when (command.customAction) {
                MpvSessionService.Commands.REFRESH_STATUS -> showRefresh(args)
                MpvSessionService.Commands.LIBRARY_STATUS ->
                    showLibrary(Protocol.decodeLibrary(args.getString("playlists")))

                MpvSessionService.Commands.BRIDGE_STATUS ->
                    onBridgeStatus(args.getBoolean("connected"))
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
        wireLibrary()
        wireMiniPlayer()
        wireSettings()
        show(Screen.NOW_PLAYING)

        requestNotificationPermissionIfNeeded()
        startService(Intent(this, MpvSessionService::class.java))
        handleSharedLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleSharedLink(intent)
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
            // The service has been running since onCreate, so everything it broadcast before now
            // was sent to nobody. Ask for the current picture.
            sendCustom(MpvSessionService.Commands.REQUEST_LIBRARY)
            scheduleAutoStartCheck()
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
        autoStartTimer.removeCallbacksAndMessages(null)
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
                R.id.nav_library -> show(Screen.LIBRARY)
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
            listFilter.doAfterTextChanged { applyPlaylistFilter() }
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
            listFilter.doAfterTextChanged { refreshFavorites() }
            // Favorites are a local list; shuffling and refreshing belong to mpv's playlist.
            listShuffle.visibility = View.GONE
            listRefresh.visibility = View.GONE
        }
    }

    private fun wireLibrary() = with(views.libraryView) {
        libraryAdapter = LibraryAdapter()
        libraryList.adapter = libraryAdapter
        libraryList.setOnItemClickListener { _, _, position, _ ->
            libraryRows.getOrNull(position)?.let(::loadPlaylist)
        }
        libraryList.setOnItemLongClickListener { _, _, position, _ ->
            libraryRows.getOrNull(position)?.let(::confirmRemovePlaylist)
            true
        }
        libraryAdd.setOnClickListener { promptForPlaylist(clipboardPlaylistUrl()) }
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
        settingAutoStart.isChecked = settings.autoStart
        settingResumeLast.isChecked = settings.resumeLastPlaylist

        settingAutoStart.setOnCheckedChangeListener { _, checked -> settings.autoStart = checked }
        settingResumeLast.setOnCheckedChangeListener { _, checked ->
            settings.resumeLastPlaylist = checked
        }

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
        favoritesStatus.text =
            plural(R.plurals.favorites_subtitle, favorites.all().size, favorites.all().size)
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

    // -- library -----------------------------------------------------------------------------

    private fun loadPlaylist(playlist: Protocol.SavedPlaylist) {
        if (controller == null) {
            toast(getString(R.string.library_needs_daemon))
            return
        }
        views.libraryView.libraryProgress.visibility = View.VISIBLE
        sendCustom(
            MpvSessionService.Commands.LOAD_PLAYLIST,
            Bundle().apply { putString("url", playlist.url) },
        )
        toast(getString(R.string.library_loading, playlist.displayTitle))
        show(Screen.NOW_PLAYING)
    }

    private fun confirmRemovePlaylist(playlist: Protocol.SavedPlaylist) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.library_remove_title, playlist.displayTitle))
            .setMessage(R.string.library_remove_message)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.library_remove_button) { _, _ ->
                sendCustom(
                    MpvSessionService.Commands.REMOVE_PLAYLIST,
                    Bundle().apply { putString("url", playlist.url) },
                )
            }
            .show()
    }

    /**
     * Ask for a playlist link and hand it to the daemon, which names it with yt-dlp.
     *
     * The name is not guessed here: only yt-dlp knows a link is called "share", and it is the
     * same call that fills the cache, so asking for the name costs nothing extra.
     */
    private fun promptForPlaylist(prefill: String?) {
        val input = EditText(this).apply {
            setHint(R.string.library_add_hint)
            setText(prefill.orEmpty())
            setSingleLine()
            setPadding(48, 32, 48, 32)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.library_add_title)
            .setMessage(R.string.library_add_message)
            .setView(input)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.library_add_button) { _, _ ->
                addPlaylist(input.text.toString().trim())
            }
            .show()
    }

    private fun addPlaylist(url: String) {
        if (!looksLikePlaylist(url)) {
            toast(getString(R.string.library_not_a_playlist))
            return
        }
        if (controller == null) {
            toast(getString(R.string.library_needs_daemon))
            return
        }
        views.libraryView.libraryProgress.visibility = View.VISIBLE
        sendCustom(
            MpvSessionService.Commands.ADD_PLAYLIST,
            Bundle().apply { putString("url", url) },
        )
        show(Screen.LIBRARY)
    }

    /** A shared link arrives as loose text with a URL somewhere in it. */
    private fun handleSharedLink(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        val shared = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return
        val url = shared.split(Regex("\\s+")).firstOrNull(::looksLikePlaylist) ?: run {
            toast(getString(R.string.library_not_a_playlist))
            return
        }
        // Consume it, so a rotation does not offer the same link again.
        intent.removeExtra(Intent.EXTRA_TEXT)
        AlertDialog.Builder(this)
            .setTitle(R.string.library_add_title)
            .setMessage(getString(R.string.library_shared, url))
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.library_add_button) { _, _ -> addPlaylist(url) }
            .show()
    }

    private fun clipboardPlaylistUrl(): String? {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
        val text = clipboard?.primaryClip?.takeIf { it.itemCount > 0 }
            ?.getItemAt(0)?.coerceToText(this)?.toString()?.trim()
        return text?.takeIf(::looksLikePlaylist)
    }

    private fun looksLikePlaylist(candidate: String): Boolean =
        candidate.startsWith("http") && candidate.contains("list=")

    // -- launching ---------------------------------------------------------------------------

    /**
     * Start the daemon in Termux when nothing is listening, so opening the app is enough.
     *
     * A short wait first: the socket is usually already up, and the service needs a moment to
     * report either way. Doing this from the activity rather than the service is deliberate --
     * Android 12+ refuses a foreground-service start from the background, and the app being open
     * is exactly what makes this call legal.
     */
    private fun scheduleAutoStartCheck() {
        if (!settings.autoStart || autoStartDone) return
        autoStartTimer.removeCallbacksAndMessages(null)
        autoStartTimer.postDelayed({
            if (bridgeConnected == true || autoStartDone) return@postDelayed
            if (!TermuxLauncher.isInstalled(this)) {
                Log.i(TAG, "nothing on ${settings.host}:${settings.port} and Termux is not here")
                return@postDelayed
            }
            autoStartDone = true
            Log.i(TAG, "nothing listening; starting the daemon in Termux")
            toast(getString(R.string.launch_auto, settings.host, settings.port))
            startInTermux()
        }, AUTO_START_DELAY_MS)
    }

    /**
     * Say so when Termux accepted the request but no daemon appeared.
     *
     * Termux refuses a plugin command asynchronously -- `startForegroundService` returns fine and
     * the refusal arrives later as a Termux notification -- so the only thing this app can
     * observe is that the socket never came up. Silence here is the failure that is hardest to
     * diagnose, and the cause is almost always the same one.
     */
    private fun watchForLaunchFailure() {
        autoStartTimer.postDelayed({
            if (bridgeConnected == true) return@postDelayed
            val message = getString(R.string.launch_no_socket, settings.port)
            Log.w(TAG, message)
            views.settingsView.launchStatus.text = message
            toast(message)
        }, LAUNCH_GRACE_MS)
    }

    private fun onBridgeStatus(connected: Boolean) {
        bridgeConnected = connected
        // Deliberately not rescheduling on a disconnect. BridgeClient retries about once a
        // second at first, and each retry reports a status -- pushing the check back every time
        // would mean it never ran.
        if (connected) autoStartTimer.removeCallbacksAndMessages(null)
        renderSubtitle()
    }

    private fun startInTermux() {
        persistConnectionSettings()
        if (TermuxLauncher.isInstalled(this) && !TermuxLauncher.hasPermission(this)) {
            // Ask the first time it is actually needed rather than on first launch, so the
            // dialog arrives with a reason the user can see.
            launchAfterPermission = true
            requestTermuxPermission.launch(TermuxLauncher.PERMISSION)
            return
        }
        val message = when (val result = TermuxLauncher.run(this, settings.launchCommand)) {
            TermuxLauncher.Result.Started -> {
                watchForLaunchFailure()
                getString(R.string.launch_started)
            }
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
        views.libraryView.root.visibility = visibleIf(target == Screen.LIBRARY)
        views.playlistView.root.visibility = visibleIf(target == Screen.PLAYLIST)
        views.favoritesView.root.visibility = visibleIf(target == Screen.FAVORITES)
        views.settingsView.root.visibility = visibleIf(target == Screen.SETTINGS)

        views.toolbar.title = getString(
            when (target) {
                Screen.NOW_PLAYING -> R.string.nav_now_playing
                Screen.LIBRARY -> R.string.nav_library
                Screen.PLAYLIST -> R.string.nav_playlist
                Screen.FAVORITES -> R.string.nav_favorites
                Screen.SETTINGS -> R.string.nav_settings
            }
        )
        if (target == Screen.FAVORITES) refreshFavorites()
        if (target != Screen.SETTINGS) persistConnectionSettings()
        renderSubtitle()
    }

    /**
     * The toolbar's second line: which playlist is loaded, or that nothing is listening.
     *
     * The playing playlist is whichever library row the daemon flagged `current`, so naming it
     * costs no extra message.
     */
    private fun renderSubtitle() {
        views.toolbar.subtitle = when {
            bridgeConnected == false -> getString(R.string.waiting_for_mpv)
            screen == Screen.NOW_PLAYING || screen == Screen.PLAYLIST ->
                libraryRows.firstOrNull { it.current }?.displayTitle.orEmpty()

            else -> ""
        }
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
            // A daemon that is up but idle is not the same as no daemon at all, and the fix for
            // each is different: pick a playlist, versus start mpvbridge.
            if (bridgeConnected == true) getString(R.string.nothing_loaded)
            else getString(R.string.waiting_for_mpv)
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
        allPlaylistRows.clear()
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
            allPlaylistRows.add(
                Row(
                    mediaIndex = i,
                    title = item.mediaMetadata.title?.toString().orEmpty(),
                    current = isCurrent,
                )
            )
        }
        hiddenUnavailable = hidden
        applyPlaylistFilter()
    }

    /** Narrow the playlist to the filter box. 855 rows is a lot to scroll past. */
    private fun applyPlaylistFilter() {
        val query = views.playlistView.listFilter.text.toString().trim()
        playlistRows.clear()
        if (query.isEmpty()) {
            playlistRows.addAll(allPlaylistRows)
        } else {
            allPlaylistRows.filterTo(playlistRows) { it.title.contains(query, ignoreCase = true) }
        }
        playlistAdapter.notifyDataSetChanged()

        views.playlistView.listSubtitle.text = when {
            allPlaylistRows.isEmpty() -> ""
            query.isNotEmpty() ->
                getString(R.string.filter_subtitle, playlistRows.size, allPlaylistRows.size)

            hiddenUnavailable > 0 -> plural(
                R.plurals.playlist_subtitle_hidden,
                playlistRows.size,
                playlistRows.size,
                hiddenUnavailable,
            )

            else -> plural(R.plurals.playlist_subtitle, playlistRows.size, playlistRows.size)
        }
        views.playlistView.listEmpty.visibility = visibleIf(playlistRows.isEmpty())
        views.playlistView.listEmpty.text = getString(
            if (allPlaylistRows.isNotEmpty()) R.string.filter_no_matches else R.string.playlist_empty
        )
    }

    private fun refreshFavorites() {
        val all = favorites.all()
        val query = views.favoritesView.listFilter.text.toString().trim()
        favoriteRows.clear()
        if (query.isEmpty()) {
            favoriteRows.addAll(all)
        } else {
            all.filterTo(favoriteRows) {
                it.title.contains(query, ignoreCase = true) ||
                    it.artist?.contains(query, ignoreCase = true) == true
            }
        }
        favoritesAdapter.notifyDataSetChanged()

        views.favoritesView.listSubtitle.text = if (query.isEmpty()) {
            plural(R.plurals.favorites_subtitle, all.size, all.size)
        } else {
            getString(R.string.filter_subtitle, favoriteRows.size, all.size)
        }
        views.favoritesView.listEmpty.visibility = visibleIf(favoriteRows.isEmpty())
        views.favoritesView.listEmpty.text = getString(
            if (all.isNotEmpty()) R.string.filter_no_matches else R.string.favorites_empty
        )
        views.settingsView.favoritesStatus.text =
            plural(R.plurals.favorites_subtitle, all.size, all.size)
    }

    private fun showLibrary(playlists: List<Protocol.SavedPlaylist>) {
        libraryRows.clear()
        libraryRows.addAll(playlists)
        libraryAdapter.notifyDataSetChanged()
        with(views.libraryView) {
            librarySubtitle.text =
                plural(R.plurals.library_subtitle, libraryRows.size, libraryRows.size)
            libraryEmpty.visibility = visibleIf(libraryRows.isEmpty())
            libraryProgress.visibility = View.GONE
        }
        renderSubtitle()
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
        val name = args.getString("title")
        val kind = args.getString("kind") ?: Protocol.KIND_RECHECK
        views.playlistView.listProgress.visibility = visibleIf(status == "running")
        views.libraryView.libraryProgress.visibility = visibleIf(status == "running")
        val message = when (status) {
            "running" ->
                if (name.isNullOrBlank()) getString(R.string.refresh_running)
                else getString(R.string.refresh_running_named, name)

            "done" -> when (kind) {
                Protocol.KIND_ADD -> {
                    val total = args.getInt("total")
                    plural(R.plurals.library_added_count, total, name.orEmpty(), total)
                }
                // Tapping a playlist already said "Loading share…"; saying it again adds nothing.
                Protocol.KIND_LOAD -> ""
                else -> {
                    val added = args.getInt("added")
                    if (added > 0) plural(R.plurals.refresh_added, added, added)
                    else getString(R.string.refresh_up_to_date)
                }
            }

            else -> getString(R.string.refresh_failed, args.getString("reason") ?: status)
        }
        if (message.isNotEmpty()) toast(message)
    }

    private fun plural(id: Int, quantity: Int, vararg args: Any): String =
        resources.getQuantityString(id, quantity, *args)

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

    private inner class LibraryAdapter : BaseAdapter() {
        override fun getCount(): Int = libraryRows.size
        override fun getItem(position: Int): Protocol.SavedPlaylist = libraryRows[position]
        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val view = convertView ?: LayoutInflater.from(parent.context)
                .inflate(R.layout.row_library, parent, false)
            val playlist = libraryRows[position]

            val title = view.findViewById<TextView>(R.id.libraryTitle)
            title.text = playlist.displayTitle
            title.setTextColor(
                ContextCompat.getColor(
                    this@MainActivity,
                    if (playlist.current) R.color.accent else R.color.text_primary
                )
            )
            view.findViewById<TextView>(R.id.librarySubtitleRow).text = when {
                playlist.count == 0 -> getString(R.string.library_row_unnamed)
                playlist.current ->
                    plural(R.plurals.library_row_playing, playlist.count, playlist.count)

                else -> plural(R.plurals.library_row_subtitle, playlist.count, playlist.count)
            }
            view.findViewById<ImageView>(R.id.libraryIcon).setImageResource(
                if (playlist.current) R.drawable.ic_now_playing else R.drawable.ic_playlist
            )
            view.setBackgroundResource(if (playlist.current) R.drawable.bg_row_current else 0)
            return view
        }
    }

    private companion object {
        const val TAG = "MpvMain"
        const val TICK_MS = 500L

        /** Long enough for a daemon that is already up to answer, short enough not to feel stuck. */
        const val AUTO_START_DELAY_MS = 2500L

        /** mpv, yt-dlp and a Python start-up need a few seconds before the socket exists. */
        const val LAUNCH_GRACE_MS = 12_000L
    }
}
