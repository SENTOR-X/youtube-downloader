# ~/youtube-downloader/app.py
import sys
import os
import re
import time
from pathlib import Path
import threading

# ---- Proje kökü: core importu için garanti ----
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

APP_ID = "com.sentor.youtubedownloader"

# ---- venv site-packages ekle (menüden açınca bazen lazım olur) ----
_venv_lib = BASE_DIR / "venv" / "lib"
if _venv_lib.exists():
    for sp in sorted(_venv_lib.glob("python*/site-packages")):
        if sp.exists():
            sys.path.insert(0, str(sp))
            break

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib, Pango, Gio

from core.formats import (
    get_formats,
    probe_playlist,
    get_formats_for_playlist_item,
    first_index_from_playlist_items_spec,
)
from core.downloader import download_video, prepend_tools_dir_to_path, ensure_yt_dlp_updated, FORMAT_OPTIONS

_STD_P = (144, 240, 360, 480, 720, 1080, 1440, 2160)


def run_in_thread(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def try_center_window(win: Gtk.Window):
    """
    X11'de pencereyi ortalamayı dener.
    Wayland'da compositor izin vermez -> hiçbir şey yapmadan çıkar.
    """
    try:
        display = Gdk.Display.get_default()
        if not display:
            return False

        name = ""
        try:
            name = (display.get_name() or "").lower()
        except Exception:
            pass

        if "x11" not in name:
            return False

        surface = win.get_surface()
        if not surface or not hasattr(surface, "move"):
            return False

        monitor = display.get_monitor_at_surface(surface) or display.get_primary_monitor()
        if not monitor:
            return False

        geo = monitor.get_geometry()
        w = win.get_allocated_width()
        h = win.get_allocated_height()
        if w <= 1 or h <= 1:
            return True

        x = geo.x + max(0, (geo.width - w) // 2)
        y = geo.y + max(0, (geo.height - h) // 2)
        surface.move(x, y)
        return False
    except Exception:
        return False



def _safe_source_remove(source_id: int) -> None:
    """Remove a GLib source id without spurious warnings if it already disappeared."""
    if not source_id:
        return
    try:
        ctx = GLib.main_context_default()
        # Some GLib builds expose find_source_by_id; guard just in case.
        finder = getattr(ctx, "find_source_by_id", None)
        if callable(finder):
            if finder(source_id) is None:
                return
        GLib.source_remove(source_id)
    except Exception:
        pass

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app)
        # Flatpak dahil: yerel tools dizinindeki yt-dlp'nin tüm subprocess çağrılarınca kullanılabilmesi için
        prepend_tools_dir_to_path()

        header = Adw.HeaderBar()
        self.set_titlebar(header)

        self.set_title("YouTube Downloader")
        self.set_default_size(860, 460)

        # Window background (glass için)
        self.add_css_class("transparent-window")
        self._install_css()
        self._schedule_ytdlp_auto_update()

        # ---- state ----
        self.output_dir = str(Path.home() / "Downloads")
        self.available_format_keys: list[str] = []
        self.last_scanned_url: str = ""
        self.current_url: str = ""  # gerçek URL (entry bazen başlık gösterir)
        self.current_title: str = ""  # son taranan başlık
        self._last_speed_mbps: float | None = None
        self._last_eta: str | None = None
        self.cancel_event: threading.Event | None = None
        self._ignore_progress_updates: bool = False
        self.last_caps: dict | None = None
        self._format_overrides: dict[str, str] = {}

        # Format seçimi: AUTO (varsayılan) durumunu takip etmek için
        self._auto_default_index: int | None = None
        self._user_picked_format: bool = False
        self._setting_selected_programmatically: bool = False

        # busy
        self._busy_reasons: set[str] = set()

        # toast spam azaltma
        self._last_toast_sig: str = ""
        self._last_toast_t: float = 0.0

        # Playlist önerisi/toast dedup (aynı URL için tekrar tekrar göstermeyelim)
        self._playlist_suggested_url: str = ""

        # Playlist indirme ilerleme durumu (örn. 2/5)
        self._pl_active: bool = False
        self._pl_selected_total: int = 0
        self._pl_ord: int = 0

        # ---- UI root ----
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.add_css_class("root-bg")

        glass = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=16,
            margin_bottom=18,
            margin_start=12,
            margin_end=12,
        )
        glass.set_hexpand(True)
        glass.set_vexpand(True)
        glass.add_css_class("glass-panel")
        root.append(glass)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(840)
        clamp.set_tightening_threshold(700)
        clamp.set_hexpand(True)
        clamp.set_halign(Gtk.Align.FILL)
        glass.append(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        main_box.set_hexpand(True)
        main_box.set_valign(Gtk.Align.START)
        main_box.set_margin_bottom(14)
        clamp.set_child(main_box)

        group = Adw.PreferencesGroup()
        main_box.append(group)

        # 1) URL
        url_row = Adw.ActionRow(title="YouTube URL")
        self.url_entry = Gtk.Entry()
        self.url_entry.set_placeholder_text("https://www.youtube.com/watch?v=...")
        # Yapıştır düğmesi ile odak alınca tüm metin seçilmesin (gölge/selection oluşmasın)
        try:
            self.url_entry.set_property('select-on-focus', False)
        except Exception:
            pass
        self.url_entry.set_hexpand(True)

        self.url_entry.add_css_class("url-entry")  # cam 60
        self.url_entry.connect("changed", self._on_url_entry_changed)
        # Sağ tarafa: Yapıştır düğmesi + URL giriş kutusu
        self.paste_button = Gtk.Button(label="Yapıştır")
        self.paste_button.connect("clicked", self.on_paste_clicked)

        url_suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        url_suffix.set_hexpand(True)
        url_suffix.set_halign(Gtk.Align.FILL)
        url_suffix.append(self.paste_button)
        self.url_entry.set_hexpand(True)
        self.url_entry.set_halign(Gtk.Align.FILL)
        url_suffix.append(self.url_entry)

        url_row.add_suffix(url_suffix)
        url_row.set_activatable_widget(self.url_entry)

        # Enter (Return) ile format tarama
        self.url_entry.connect("activate", self.on_scan_formats_clicked)

        # 2) Format Tara (full width) - ikon + label + mini spinner
        self.scan_button = Gtk.Button()
        self.scan_button.set_hexpand(True)
        self.scan_button.set_halign(Gtk.Align.FILL)
        self.scan_button.add_css_class("pill")
        self.scan_button.connect("clicked", self.on_scan_formats_clicked)

        self.scan_btn_icon = Gtk.Image.new_from_icon_name("system-search-symbolic")
        self.scan_btn_icon.set_pixel_size(16)

        self.scan_btn_label = Gtk.Label(label="Format Tara")
        self.scan_btn_spinner = Gtk.Spinner()
        self.scan_btn_spinner.set_visible(False)
        self.scan_btn_spinner.set_halign(Gtk.Align.CENTER)

        scan_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        scan_btn_box.set_halign(Gtk.Align.CENTER)
        scan_btn_box.append(self.scan_btn_icon)
        scan_btn_box.append(self.scan_btn_label)
        scan_btn_box.append(self.scan_btn_spinner)
        self.scan_button.set_child(scan_btn_box)

        scan_row = Adw.PreferencesRow()
        scan_row.set_child(self.scan_button)

        # 3) Format ComboRow
        self.format_model = Gtk.StringList.new(["(Önce 'Format Tara')"])
        self.format_row = Adw.ComboRow(title="Format", model=self.format_model)
        self.format_row.set_sensitive(False)
        self.format_row.connect("notify::selected", self._on_format_selected_changed)
        self._setup_format_item_factory()  # modern icon + label + badge

        # 4) Klasör
        folder_row = Adw.ActionRow(title="İndirme Klasörü")
        self.folder_label = Gtk.Label(label=self.output_dir, xalign=0)
        folder_button = Gtk.Button()
        folder_button.add_css_class("pill")
        folder_button.set_child(self._button_content("Klasör Seç", "folder-open-symbolic"))
        folder_button.connect("clicked", self.on_select_folder)
        folder_row.add_suffix(self.folder_label)
        folder_row.add_suffix(folder_button)

        # 5) İndir (full width) - primary
        self.download_button = Gtk.Button()
        self.download_button.set_hexpand(True)
        self.download_button.set_halign(Gtk.Align.FILL)
        self.download_button.add_css_class("download-btn")
        self.download_button.add_css_class("pill")
        self.download_button.set_child(self._button_content("İndir", "folder-download-symbolic"))
        self.download_button.set_sensitive(False)
        self.download_button.connect("clicked", self.on_download_clicked)
        download_row = Adw.PreferencesRow()
        download_row.set_child(self.download_button)

        # 6) İptal (full width) - destructive
        self.cancel_button = Gtk.Button()
        self.cancel_button.set_hexpand(True)
        self.cancel_button.set_halign(Gtk.Align.FILL)
        self.cancel_button.add_css_class("pill")

        self.cancel_button.add_css_class("cancel-btn")
        self.cancel_button.set_child(self._button_content("İptal", "process-stop-symbolic"))
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", self.on_cancel_clicked)
        cancel_row = Adw.PreferencesRow()
        cancel_row.set_child(self.cancel_button)

        # 7) Progress
        self.progress = Gtk.ProgressBar()

        self.progress.add_css_class("ytdl-progress")
        self.progress.set_fraction(0.0)
        self.progress.set_show_text(True)
        self.progress.set_text("%0")
        progress_row = Adw.PreferencesRow()
        progress_row.set_child(self.progress)

        # 8) Status: ikon + global spinner + metin
        self.status_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        self.status_icon.set_pixel_size(16)

        self.status_spinner = Gtk.Spinner()
        self.status_spinner.set_visible(False)

        self.status_label = Gtk.Label(label="Hazır", xalign=0)

        self.last_download_path: str | None = None

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_box.append(self.status_icon)
        status_box.append(self.status_spinner)
        status_box.append(self.status_label)

        status_row = Adw.ActionRow()
        status_row.set_title("")
        status_row.add_suffix(status_box)

        # ---- sıra bozulmadan group order ----
        group.add(url_row)
        group.add(scan_row)
        group.add(self.format_row)
        group.add(folder_row)
        group.add(download_row)
        group.add(cancel_row)
        group.add(progress_row)
        group.add(status_row)

        # ---- Gelişmiş (Playlist) ----
        self._playlist_meta = {}
        self._playlist_scan_item = None

        adv_group = Adw.PreferencesGroup(title="Gelişmiş")
        main_box.append(adv_group)

        # Playlist toggle
        self.playlist_switch = Gtk.Switch()
        self.playlist_switch.set_valign(Gtk.Align.CENTER)
        self.playlist_switch.set_active(False)
        self.playlist_switch.add_css_class("ytdl-switch")
        self.playlist_switch.connect("notify::active", self._on_playlist_toggle_changed)

        playlist_row = Adw.ActionRow(
            title="Playlist indir",
            subtitle="Açıksa URL playlist ise tüm öğeler indirilir (varsayılan: tek video).",
        )
        playlist_row.add_suffix(self.playlist_switch)
        playlist_row.set_activatable_widget(self.playlist_switch)
        adv_group.add(playlist_row)

        # Playlist items (optional)
        self.playlist_items_entry = Gtk.Entry()
        self.playlist_items_entry.add_css_class("ytdl-entry")
        self.playlist_items_entry.set_hexpand(True)
        self.playlist_items_entry.set_placeholder_text("Örn: 1:10,12,15 (boş: tüm playlist)")
        self.playlist_items_entry.set_sensitive(False)

        items_row = Adw.ActionRow(
            title="Playlist öğeleri",
            subtitle="Opsiyonel: İndirilecek öğeleri seç (yt-dlp --playlist-items).",
        )
        items_row.add_suffix(self.playlist_items_entry)
        adv_group.add(items_row)

        # Playlist info (read-only)
        self.playlist_info_row = Adw.ActionRow(
            title="Playlist bilgisi",
            subtitle="—",
        )
        self.playlist_info_row.set_sensitive(False)
        adv_group.add(self.playlist_info_row)

        # ---- ToastOverlay ----
        self.toast_overlay = Adw.ToastOverlay()
        self._persist_toast_timeout_s = 86400  # ~1 day; dismissed manually for persistent toasts
        self.toast_overlay.set_child(root)
        self.set_child(self.toast_overlay)

        # ---- Network monitor (download sırasında bağlantı kesilince uyarı) ----
        # Not: Gerçek "cam" şeffaflık açıkken, bağlantı kesilmesi yt-dlp tarafında hata üretebilir.
        # Kullanıcıyı panikletmemek için "worker hatası" metnini bastırıp, network durumunu toast ile yönetiyoruz.
        self._net_down_timer_id: int = 0
        self._net_down_toast = None  # type: Adw.Toast | None
        self._net_down_toast_shown: bool = False
        self._net_cancel_toast = None  # type: Adw.Toast | None
        self._download_complete_toast = None  # type: Adw.Toast | None
        self._net_available: bool = True
        self._net_was_down_during_download: bool = False
        self._download_failed_due_to_net: bool = False
        self._net_check_timer_id: int = 0
        self._net_check_started_us: int = 0
        self._net_check_escalated: bool = False
        try:
            self._netmon = Gio.NetworkMonitor.get_default()
            self._net_available = self._compute_net_available()
            self._netmon.connect("network-changed", self._on_network_changed)
            # Bazı ortamlarda (özellikle sandbox) connectivity değişimi daha iyi sinyal verebiliyor.
            try:
                self._netmon.connect("notify::connectivity", self._on_net_connectivity_notify)
            except Exception:
                pass
        except Exception:
            self._netmon = None
            self._net_available = True

        self.set_status("info", "Hazır")

    # ---------- yt-dlp auto update ----------
    def _schedule_ytdlp_auto_update(self) -> None:
        """
        Uygulama açılışında UI donmadan yt-dlp güncellemesini tetikler.
        Not: ensure_yt_dlp_updated() zaten günlük kontrol (throttle) uygular.
        """
        if getattr(self, "_ytdlp_auto_update_source_id", 0):
            return  # zaten planlanmış

        # UI ilk çizilsin diye kısa gecikme
        def _kick() -> bool:
            self._ytdlp_auto_update_source_id = 0
            self._start_ytdlp_auto_update(force=False)
            return False  # one-shot

        self._ytdlp_auto_update_source_id = GLib.timeout_add_seconds(2, _kick)

    def _start_ytdlp_auto_update(self, *, force: bool = False) -> None:
        if getattr(self, "_ytdlp_auto_update_running", False):
            return
        self._ytdlp_auto_update_running = True

        def worker():
            result = {"updated": False, "old": None, "new": None, "path": None}
            try:
                result = ensure_yt_dlp_updated(force=force)
            except Exception:
                # Sessiz geç: uygulama açılışını asla bozmasın
                pass

            def ui_done():
                self._ytdlp_auto_update_running = False
                try:
                    if result.get("updated"):
                        old = (result.get("old") or "").strip()
                        new = (result.get("new") or "").strip()
                        if old and new:
                            self.show_toast("ok", f"yt-dlp güncellendi: {old} → {new}", timeout_s=4)
                        else:
                            self.show_toast("ok", "yt-dlp güncellendi", timeout_s=4)
                except Exception:
                    pass
                return False

            GLib.idle_add(ui_done)

        run_in_thread(worker)

    # ---------- UI helpers ----------
    def _button_content(self, label: str, icon_name: str) -> Gtk.Widget:
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        lab = Gtk.Label(label=label)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_halign(Gtk.Align.CENTER)
        box.append(icon)
        box.append(lab)
        return box

    def _dismiss_toast(self, toast):
        if not toast:
            return
        # Preferred: Adw.Toast has dismiss()
        try:
            if hasattr(toast, "dismiss"):
                toast.dismiss()
                return
        except Exception:
            pass

        overlay = getattr(self, "toast_overlay", None) or getattr(self, "_toast_overlay", None)
        if overlay is None:
            return

        for meth in ("dismiss_toast", "dismiss", "remove_toast"):
            fn = getattr(overlay, meth, None)
            if fn is None:
                continue
            try:
                fn(toast)
                return
            except Exception:
                continue

    def show_toast(self, level: str, message: str, *, timeout_s: int = 3, cooldown_s: float = 0.7, priority=None):
        """
        Sağ altta toast gösterir.
        timeout_s: saniye; 0 => otomatik kapanmaz (libadwaita davranışı).
        priority: Adw.ToastPriority (opsiyonel).
        """
        try:
            now = time.monotonic()
            sig = f"{level}:{message}"
            if self._last_toast_sig == sig and (now - self._last_toast_t) < cooldown_s:
                return None
            self._last_toast_sig = sig
            self._last_toast_t = now
            toast = Adw.Toast.new(message)
            if hasattr(toast, "set_timeout"):
                timeout = int(timeout_s)
                if timeout <= 0:
                    timeout = int(getattr(self, '_persist_toast_timeout_s', 86400))
                toast.set_timeout(timeout)
            if priority is not None and hasattr(toast, "set_priority"):
                try:
                    toast.set_priority(priority)
                except Exception:
                    pass
            self.toast_overlay.add_toast(toast)
            return toast
        except Exception:
            return None

    def show_toast_action(self, message: str, *, button_label: str, on_click, timeout_s: int = 6, cooldown_s: float = 0.7, priority=None):
        """
        Sağ altta toast gösterir ve bir aksiyon butonu ekler.
        on_click: parametresiz callable (GTK main thread içinde çağrılır)
        timeout_s: saniye; 0 => otomatik kapanmaz.
        """
        try:
            now = time.monotonic()
            sig = f"action:{message}:{button_label}"
            if self._last_toast_sig == sig and (now - self._last_toast_t) < cooldown_s:
                return None
            self._last_toast_sig = sig
            self._last_toast_t = now

            toast = Adw.Toast.new(message)
            if hasattr(toast, "set_timeout"):
                timeout = int(timeout_s)
                if timeout <= 0:
                    timeout = int(getattr(self, '_persist_toast_timeout_s', 86400))
                toast.set_timeout(timeout)
            if priority is not None and hasattr(toast, "set_priority"):
                try:
                    toast.set_priority(priority)
                except Exception:
                    pass

            # Libadwaita: toast butonuna tıklanınca "button-clicked" sinyali gelir.
            if hasattr(toast, "set_button_label"):
                toast.set_button_label(button_label)
                try:
                    toast.connect("button-clicked", lambda *_args: on_click())
                except Exception:
                    pass

            self.toast_overlay.add_toast(toast)
            return toast
        except Exception:
            return None


    def _count_playlist_items_spec(self, spec: str) -> int:
        """yt-dlp --playlist-items girdisini sayıya çevir (yaklaşık, deterministik).
        Örnekler:
          - "1:5" => 5
          - "1:10,12,15" => 12
          - "3,7,12" => 3
        Not: duplicate/overlap durumlarında (örn. "1:3,2:4") çakışmayı çözmez; pratikte kullanıcı
        böyle bir giriş yapmadığı için basit ve güvenli tutuldu.
        """
        s = (spec or "").strip()
        if not s:
            return 0
        total = 0
        for part in [p.strip() for p in s.split(",") if p.strip()]:
            if ":" in part:
                a, b = part.split(":", 1)
                try:
                    start = int(a.strip())
                    end = int(b.strip())
                except Exception:
                    continue
                if start <= 0 or end <= 0:
                    continue
                total += abs(end - start) + 1
            else:
                try:
                    v = int(part)
                except Exception:
                    continue
                if v > 0:
                    total += 1
        return max(0, int(total))

    def _playlist_suffix(self) -> str:
        if not getattr(self, "_pl_active", False):
            return ""
        ordv = int(getattr(self, "_pl_ord", 0) or 0)
        if ordv <= 0:
            return ""
        total = int(getattr(self, "_pl_selected_total", 0) or 0)
        return f" • {ordv}/{total}" if total > 0 else f" • {ordv}"

    def _reset_playlist_download_state(self) -> None:
        self._pl_active = False
        self._pl_selected_total = 0
        self._pl_ord = 0

    def _maybe_show_playlist_suggestion(self, url: str, meta: dict | None) -> bool:
        """Playlist tespit edilirse ve switch kapalıysa, kullanıcıya 'Aç' aksiyonlu toast öner."""
        try:
            if not meta or not meta.get("is_playlist"):
                return False
            if meta.get("playlist_only"):
                return False  # bu durumda zaten zorunlu uyarı veriyoruz
            if not getattr(self, "playlist_switch", None):
                return False
            if bool(self.playlist_switch.get_active()):
                return False
            u = (url or "").strip()
            if not u:
                return False
            if u == (getattr(self, "_playlist_suggested_url", "") or "").strip():
                return False

            self._playlist_suggested_url = u

            def _enable():
                try:
                    self.playlist_switch.set_active(True)
                except Exception:
                    pass

            # Aksiyonlu toast: kullanıcı isterse switch'i tek tıkla açar.
            self.show_toast_action(
                "URL playlist içeriyor. İstersen 'Playlist indir'i açabilirsin.",
                button_label="Aç",
                on_click=_enable,
                timeout_s=6,
                cooldown_s=1.2,
            )
        except Exception:
            pass
        return False

    # ---------- Busy ----------
    def _busy_push(self, reason: str):
        self._busy_reasons.add(reason)
        self._sync_busy_widgets()

    def _busy_pop(self, reason: str):
        self._busy_reasons.discard(reason)
        self._sync_busy_widgets()

    def _sync_busy_widgets(self):
        busy = bool(self._busy_reasons)

        self.status_spinner.set_visible(busy)
        if busy:
            self.status_spinner.start()
        else:
            self.status_spinner.stop()

        scan_busy = "scan" in self._busy_reasons
        self.scan_btn_spinner.set_visible(scan_busy)
        if scan_busy:
            self.scan_btn_spinner.start()
        else:
            self.scan_btn_spinner.stop()

    # ---------- Status ----------
    def set_status(self, kind: str, text: str, toast: bool = False):
        icon_map = {
            "info": "dialog-information-symbolic",
            "ok": "emblem-ok-symbolic",
            "error": "dialog-error-symbolic",
            "warn": "dialog-warning-symbolic",
            "cancel": "process-stop-symbolic",
            "download": "folder-download-symbolic",
        }
        self.status_icon.set_from_icon_name(icon_map.get(kind, "dialog-information-symbolic"))
        self.status_label.set_text(text)

        for c in ("status-info", "status-ok", "status-warn", "status-error", "status-cancel", "status-download"):
            self.status_label.remove_css_class(c)
        self.status_label.add_css_class(f"status-{kind}")

        if toast:
            self.show_toast(kind, text)


    def _suppress_worker_error_line(self):
        """Ağ kopması gibi durumlarda status alanına 'worker hatası' benzeri metin basmayı engelle."""
        # Bilerek hiçbir şey yapmıyoruz. Kullanıcıya sadece toast gösterilecek.
        return False

    def _strip_leading_symbols(self, s: str) -> str:
        s = s.strip()
        s = re.sub(r"^[^\wĞÜŞİÖÇğüşiöç]+", "", s).strip()
        return s

    def _status_from_core(self, text: str):
        # Downloader'dan playlist öğe başlangıcı bilgisi (özel prefix)
        if isinstance(text, str) and text.startswith("__PL_ITEM__:"):
            try:
                _parts = text.split(":")
                raw_i = int(_parts[1]) if len(_parts) > 1 else 0
                raw_n = int(_parts[2]) if len(_parts) > 2 else 0
            except Exception:
                raw_i, raw_n = 0, 0

            if getattr(self, "_pl_active", False):
                # Eğer yt-dlp toplamı seçili toplam ile aynı veriyorsa, doğrudan X/Y kullanabiliriz.
                try:
                    sel_total = int(getattr(self, "_pl_selected_total", 0) or 0)
                except Exception:
                    sel_total = 0

                if sel_total <= 0 and raw_n > 0:
                    self._pl_selected_total = int(raw_n)
                    sel_total = int(raw_n)

                if raw_i > 0 and raw_n > 0 and sel_total == raw_n:
                    self._pl_ord = int(raw_i)
                else:
                    # Seçili öğeler için deterministik sıra: 1,2,3,...
                    self._pl_ord = int(getattr(self, "_pl_ord", 0) or 0) + 1

                suf = self._playlist_suffix()
                self.set_status("download", f"İndiriliyor{suf}")

                # Progress metnini hemen refresh et (yüzde satırı gelmeden de UI'da görünür olsun).
                # (Bazı içeriklerde yt-dlp 'Downloading item X of Y' yazıp bir süre yüzde üretmeyebiliyor.)
                try:
                    frac = float(self.progress.get_fraction())
                    self.progress.set_show_text(True)
                    self.progress.set_text(self._format_progress_text(frac, self._last_speed_mbps, self._last_eta))
                except Exception:
                    pass
            return

        t = self._strip_leading_symbols(text)
        low = t.lower()
        # Ağ kopması / DNS gibi hatalar: status alanına panikletici satırlar basma.
        # Bunun yerine 3 sn sonra kalıcı 'Lütfen İnternet Bağlantınızı Kontrol Edin' toast'ını devreye al.
        try:
            if self._is_network_error_message(t):
                self._net_available = False
                self._net_was_down_during_download = True
                self._schedule_net_down_toast()
                return
        except Exception:
            pass
        if "iptal edildi" in low:
            self._ignore_progress_updates = True
            self._clear_progress_text()
            self.set_status("cancel", t, toast=True)
        elif "iptal" in low:
            self.set_status("cancel", t, toast=True)
        elif "tamam" in low or "bitti" in low:
            # İndirme bitti bilgisini ayrıca aksiyonlu toast ile veriyoruz; burada tekrar toast basma.
            self._ignore_progress_updates = True
            self._clear_progress_text()
            self.set_status("ok", "Hazır", toast=False)
        elif "hata" in low or "bulunamad" in low:
            self.set_status("error", t, toast=True)
        elif "indir" in low or "başlat" in low or "hazırlan" in low:
            self.set_status("download", t + self._playlist_suffix())
        else:
            self.set_status("info", t + self._playlist_suffix())

    # ---------- CSS ----------
    # ---------- CSS ----------
    def _install_css(self):
        # Create one provider and update its content on demand.
        if not hasattr(self, "_css_provider") or self._css_provider is None:
            self._css_provider = Gtk.CssProvider()
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display, self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            # Rebuild CSS when effective dark/light changes
            try:
                style = Adw.StyleManager.get_default()
                style.connect("notify::dark", lambda *_: self._reload_css())
            except Exception:
                pass
        self._reload_css()

    def _reload_css(self):
        # Build CSS without unsupported constructs (no CSS var()).
        style = Adw.StyleManager.get_default()
        try:
            use_dark = bool(style.get_dark())
        except Exception:
            try:
                use_dark = bool(style.props.dark)
            except Exception:
                use_dark = False

        # Dark palette: deterministic (Siyah).
        yd_glass_bg = "#0b0c0f"
        yd_glass_border = "#2b2e34"

        css_light = """
        /* Liquid Glass (50% - reduced transparency)
           Not: Bu modda arka plan pencereleri/masaüstü görünür. */
        .transparent-window {
            background-color: transparent;
        }

        /* Root arka plan: çok hafif tint + vignette (duvar kağıdı görünür) */
        .root-bg {
            background-color: alpha(@window_bg_color, 0.52);
            background-image: linear-gradient(135deg,
                alpha(black, 0.34),
                alpha(black, 0.20)
            );
        }

        /* Başlık çubuğu da hafif cam */
        headerbar, .titlebar {
            background-color: alpha(@window_bg_color, 0.28);
            border-bottom: 1px solid alpha(@borders, 0.35);
            box-shadow: inset 0 -1px 0 alpha(black, 0.32);
        }

        /* Ana panel: cam + iç highlight */
        .glass-panel {
            background-color: alpha(@window_bg_color, 0.44);
            background-image: linear-gradient(135deg,
                alpha(@window_bg_color, 0.60),
                alpha(@window_bg_color, 0.32)
            );
            border: 1px solid alpha(@borders, 0.44);
            border-radius: 18px;
            box-shadow:
                0 18px 42px alpha(black, 0.38),
                inset 0 1px 0 alpha(white, 0.10),
                inset 0 -1px 0 alpha(black, 0.25);
        }

        /* Adw.PreferencesGroup iç kutusu (boxed-list) cam görünüm */
        .boxed-list {
            background-color: alpha(@window_bg_color, 0.36);
            background-image: linear-gradient(180deg,
                alpha(white, 0.06),
                alpha(black, 0.16)
            );
            border: 1px solid alpha(@borders, 0.28);
            border-radius: 12px;
        }
        .boxed-list > row {
            background-color: transparent;
        }

        /* Etiket/badge */
        .badge {
            background-color: alpha(@window_bg_color, 0.22);
            border: 1px solid alpha(@borders, 0.18);
            border-radius: 999px;
            padding: 2px 8px;
        }

        /* Pill butonlar: cam */
        button.pill:not(.download-ready):not(.download-btn) {
            background-color: alpha(@window_bg_color, 0.36);
            border: 1px solid alpha(@borders, 0.26);
            border-radius: 999px;
            padding: 10px 14px;
        }
        button.pill:not(.download-ready):not(.download-btn):hover {
            background-color: alpha(@window_bg_color, 0.48);
        }

        /* Download butonu: sabit (cam değil) */
        button.download-btn {
            border-radius: 999px;
            padding: 10px 14px;
        }

        /* URL giriş alanı: cam 60 (her daim) */
        entry.url-entry {
            background-color: alpha(@window_bg_color, 0.40); /* cam 60 */
            border: 1px solid alpha(@borders, 0.30);
            border-radius: 10px;
            box-shadow: inset 0 1px 0 alpha(white, 0.10);
            color: @window_fg_color;
        }
        entry.url-entry:focus {
            background-color: alpha(@window_bg_color, 0.46);
            /* Flatpak'te aksan rengi (mavi) yerine deterministik nötr odak çizgisi */
            border: 1px solid alpha(black, 0.24);
        }

        /* Playlist öğeleri vb. diğer giriş alanları: url-entry ile aynı cam hissi */
        entry.ytdl-entry {
            background-color: alpha(@window_bg_color, 0.40);
            border: 1px solid alpha(@borders, 0.30);
            border-radius: 10px;
            box-shadow: inset 0 1px 0 alpha(white, 0.10);
            color: @window_fg_color;
        }
        entry.ytdl-entry:focus {
            background-color: alpha(@window_bg_color, 0.46);
            border: 1px solid alpha(black, 0.24);
        }

        /* Switch: Flatpak'teki mavi aksanı bastır (deterministik gri) */
        switch.ytdl-switch {
            border: 1px solid alpha(@borders, 0.30);
            background-color: alpha(@window_bg_color, 0.34);
            border-radius: 999px;
        }
        switch.ytdl-switch:checked {
            background-color: alpha(black, 0.22);
            border-color: alpha(black, 0.22);
        }
        switch.ytdl-switch slider {
            background-color: alpha(white, 0.86);
            border: 1px solid alpha(black, 0.16);
            border-radius: 999px;
        }

        /* Format seçim popover: dış katmanı gizle, iç camı artır */
        popover.background, popover {
            background-color: transparent;
            border: none;
            box-shadow: none;
            padding: 0;
            border-radius: 0;
        }
        popover.background > contents, popover > contents {
            background-color: alpha(@window_bg_color, 0.60); /* cam ~40-45 */
            border: 1px solid alpha(@borders, 0.28);
            border-radius: 16px;
            box-shadow: 0 14px 34px alpha(black, 0.38);
        }

        /* Progress bar: mavi olmasın; indir butonu rengiyle aynı (beyaz) + cam 50 */
        @keyframes ytdl_shine {
            from { background-position: 0% 0%; }
            to   { background-position: 200% 0%; }
        }
        progressbar.ytdl-progress trough {
            background-color: alpha(@window_bg_color, 0.30);
            border: 1px solid alpha(@borders, 0.22);
            border-radius: 999px;
            min-height: 10px;
        }
        progressbar.ytdl-progress progress {
            background-color: alpha(#ffffff, 0.46);
            /* Hareketli parlayan şerit (Flatpak/Adwaita farklarını sabitlemek için) */
            background-image: linear-gradient(90deg,
                alpha(#ffffff, 0.08) 0%,
                alpha(#ffffff, 0.28) 20%,
                alpha(#ffffff, 0.08) 40%,
                alpha(#ffffff, 0.08) 100%
            );
            background-size: 200% 100%;
            animation: ytdl_shine 1.25s linear infinite;
            border-radius: 999px;
            box-shadow: inset 0 1px 0 alpha(white, 0.18);
        }
        progressbar.ytdl-progress text {
            color: alpha(@window_fg_color, 0.70);
        }

        /* İptal butonu: sadece indirme esnasında cam + kırmızı */
        button.cancel-btn.ytdl-cancel-hot {
            background-color: alpha(#ff3b30, 0.16);
            background-image: linear-gradient(135deg,
                alpha(#ff3b30, 0.30),
                alpha(#ff3b30, 0.12)
            );
            border: 1px solid alpha(#ff3b30, 0.34);
            color: #ffffff;
        }
        button.cancel-btn.ytdl-cancel-hot:hover {
            background-color: alpha(#ff3b30, 0.22);
        }"""

        css_dark = f"""
        @define-color yd_glass_bg {yd_glass_bg};
        @define-color yd_glass_border {yd_glass_border};
        @define-color yd_glass_fg #ffffff;

        /* Liquid Glass (50% - reduced transparency)
           Not: Bu modda arka plan pencereleri/masaüstü görünür. */
        .transparent-window {{
            background-color: transparent;
        }}

        /* Root arka plan: çok hafif tint + vignette (duvar kağıdı görünür) */
        .root-bg {{
            background-color: alpha(@yd_glass_bg, 0.52);
            background-image: linear-gradient(135deg,
                alpha(black, 0.34),
                alpha(black, 0.20)
            );
        }}

        /* Başlık çubuğu da hafif cam */
        headerbar, .titlebar {{
            background-color: alpha(@yd_glass_bg, 0.28);
            border-bottom: 1px solid alpha(@yd_glass_border, 0.35);
            box-shadow: inset 0 -1px 0 alpha(black, 0.32);
        }}

        /* Ana panel: cam + iç highlight */
        .glass-panel {{
            background-color: alpha(@yd_glass_bg, 0.44);
            background-image: linear-gradient(135deg,
                alpha(@yd_glass_bg, 0.60),
                alpha(@yd_glass_bg, 0.32)
            );
            border: 1px solid alpha(@yd_glass_border, 0.44);
            border-radius: 18px;
            box-shadow:
                0 18px 42px alpha(black, 0.38),
                inset 0 1px 0 alpha(white, 0.10),
                inset 0 -1px 0 alpha(black, 0.25);
        }}

        /* Adw.PreferencesGroup iç kutusu (boxed-list) cam görünüm */
        .boxed-list {{
            background-color: alpha(@yd_glass_bg, 0.36);
            background-image: linear-gradient(180deg,
                alpha(white, 0.06),
                alpha(black, 0.16)
            );
            border: 1px solid alpha(@yd_glass_border, 0.28);
            border-radius: 12px;
        }}
        .boxed-list > row {{
            background-color: transparent;
        }}

        /* Etiket/badge */
        .badge {{
            background-color: alpha(@yd_glass_bg, 0.44);
            border: 1px solid alpha(@yd_glass_border, 0.28);
            border-radius: 999px;
            padding: 2px 8px;
        }}

        /* Pill butonlar: cam */
        button.pill:not(.download-ready):not(.download-btn) {{
            background-color: alpha(@yd_glass_bg, 0.46);
            border: 1px solid alpha(@yd_glass_border, 0.34);
            color: @yd_glass_fg;
            border-radius: 999px;
            padding: 10px 14px;
        }}
        button.pill:not(.download-ready):not(.download-btn):hover {{
            background-color: alpha(@yd_glass_bg, 0.58);
        }}

        /* Download butonu: cam değil -> deterministic */
        button.download-btn {{
            background-color: alpha(#ffffff, 0.14);
            border: 1px solid alpha(@yd_glass_border, 0.24);
            color: #ffffff;
            border-radius: 999px;
            padding: 10px 14px;
        }}
        button.download-btn:hover {{
            background-color: alpha(#ffffff, 0.20);
        }}

        /* URL giriş alanı: cam 60 (her daim) */
        entry.url-entry {{
            background-color: alpha(@yd_glass_bg, 0.40); /* cam 60 */
            border: 1px solid alpha(@yd_glass_border, 0.30);
            border-radius: 10px;
            box-shadow: inset 0 1px 0 alpha(white, 0.10);
            color: @yd_glass_fg;
        }}
        entry.url-entry:focus {{
            background-color: alpha(@yd_glass_bg, 0.46);
            border: 1px solid alpha(#ffffff, 0.28);
        }}

        /* Playlist öğeleri vb. diğer giriş alanları */
        entry.ytdl-entry {{
            background-color: alpha(@yd_glass_bg, 0.40);
            border: 1px solid alpha(@yd_glass_border, 0.30);
            border-radius: 10px;
            box-shadow: inset 0 1px 0 alpha(white, 0.10);
            color: @yd_glass_fg;
        }}
        entry.ytdl-entry:focus {{
            background-color: alpha(@yd_glass_bg, 0.46);
            border: 1px solid alpha(#ffffff, 0.28);
        }}

        /* Switch: deterministik (mavi aksanı bastır) */
        switch.ytdl-switch {{
            border: 1px solid alpha(@yd_glass_border, 0.34);
            background-color: alpha(@yd_glass_bg, 0.34);
            border-radius: 999px;
        }}
        switch.ytdl-switch:checked {{
            background-color: alpha(#ffffff, 0.18);
            border-color: alpha(#ffffff, 0.14);
        }}
        switch.ytdl-switch slider {{
            background-color: alpha(#ffffff, 0.86);
            border: 1px solid alpha(#000000, 0.22);
            border-radius: 999px;
        }}

        /* Format seçim popover: dış katmanı gizle, iç camı artır */
        popover.background, popover {{
            background-color: transparent;
            border: none;
            box-shadow: none;
            padding: 0;
            border-radius: 0;
        }}
        popover.background > contents, popover > contents {{
            background-color: alpha(@yd_glass_bg, 0.60); /* cam ~40-45 */
            border: 1px solid alpha(@yd_glass_border, 0.28);
            border-radius: 16px;
            box-shadow: 0 14px 34px alpha(black, 0.38);
        }}

        /* Progress bar: mavi olmasın; indir butonu rengiyle aynı (beyaz) + cam 50 */
        @keyframes ytdl_shine {{
            from {{ background-position: 0% 0%; }}
            to   {{ background-position: 200% 0%; }}
        }}
        progressbar.ytdl-progress trough {{
            background-color: alpha(@yd_glass_bg, 0.30);
            border: 1px solid alpha(@yd_glass_border, 0.22);
            border-radius: 999px;
            min-height: 10px;
        }}
        progressbar.ytdl-progress progress {{
            background-color: alpha(#ffffff, 0.46);
            background-image: linear-gradient(90deg,
                alpha(#ffffff, 0.08) 0%,
                alpha(#ffffff, 0.28) 20%,
                alpha(#ffffff, 0.08) 40%,
                alpha(#ffffff, 0.08) 100%
            );
            background-size: 200% 100%;
            animation: ytdl_shine 1.25s linear infinite;
            border-radius: 999px;
            box-shadow: inset 0 1px 0 alpha(white, 0.18);
        }}
        progressbar.ytdl-progress text {{
            color: alpha(@yd_glass_fg, 0.70);
        }}

        /* İptal butonu: sadece indirme esnasında cam + kırmızı */
        button.cancel-btn.ytdl-cancel-hot {{
            background-color: alpha(#ff3b30, 0.16);
            background-image: linear-gradient(135deg,
                alpha(#ff3b30, 0.30),
                alpha(#ff3b30, 0.12)
            );
            border: 1px solid alpha(#ff3b30, 0.34);
            color: #ffffff;
        }}
        button.cancel-btn.ytdl-cancel-hot:hover {{
            background-color: alpha(#ff3b30, 0.22);
        }}"""

        css = css_dark if use_dark else css_light
        try:
            self._css_provider.load_from_data(css.encode("utf-8"))
        except Exception:
            self._css_provider.load_from_data(bytes(css, "utf-8"))

    def _set_download_ready(self, ready: bool):
        # Formatlar hazır olduğunda: sadece İndir butonu görsel olarak hazır hale gelsin.
        if ready:
            self.download_button.add_css_class("download-ready")
        else:
            self.download_button.remove_css_class("download-ready")
    def _format_progress_text(self, frac: float, speed_mbps: float | None, eta: str | None) -> str:
        pct = int(frac * 100)
        parts: list[str] = []

        # Playlist indirmelerinde (switch açık) kaçıncı öğe bilgisi: 2/5 gibi
        if getattr(self, "_pl_active", False):
            ordv = int(getattr(self, "_pl_ord", 0) or 0)
            total = int(getattr(self, "_pl_selected_total", 0) or 0)
            if ordv > 0:
                parts.append(f"{ordv}/{total}" if total > 0 else f"{ordv}")

        # Yüzde bilgisi
        parts.append(f"%{pct}")

        if speed_mbps is not None and speed_mbps > 0:
            parts.append(f"{speed_mbps:.1f} Mb/sn")
        if eta and eta.lower() != "unknown" and eta != "--:--":
            parts.append(f"Kalan {eta}")
        return "  •  ".join(parts)

    def _set_progress(self, frac: float, speed_mbps: float | None = None, eta: str | None = None):
        # İptal sonrası gelebilecek "gecikmeli" progress güncellemelerini yok say.
        if getattr(self, "_ignore_progress_updates", False):
            return

        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac

        if speed_mbps is not None:
            self._last_speed_mbps = speed_mbps
        if eta is not None:
            self._last_eta = eta

        self.progress.set_show_text(True)
        self.progress.set_fraction(frac)
        self.progress.set_text(self._format_progress_text(frac, self._last_speed_mbps, self._last_eta))

    def _clear_progress_text(self):
        # İptal durumunda yüzde/hız/ETA metnini tamamen gizle.
        self._last_speed_mbps = None
        self._last_eta = None
        self.progress.set_text("")
        self.progress.set_show_text(False)

    def _set_last_download_path(self, path):
        # path: str | None
        if isinstance(path, str):
            path = path.strip() or None
        self.last_download_path = path
        # 'İndirme tamamlandı / Göster' toast'ı yalnızca *başarılı indirme* sonrasında
        # üretilir. URL entry değişince path=None ile burası çağrılabilir; o durumda
        # _last_download_toast_url'yi güncellemek, farklı link taramasında toast'ın
        # kapanmasını engeller (bug).
        if path:
            # Bu toast'ın hangi URL için üretildiğini sakla (farklı link taramasında otomatik temizlemek için)
            self._last_download_toast_url = (getattr(self, "current_url", "") or "").strip()
            self._toast_download_complete(path)

    def _toast_download_complete(self, path: str):
        # "Göster" aksiyonlu toast: yeni bir indirme/format taramaya kadar ekranda kalsın.
        # Önce varsa önceki ağ-iptal toast'ını kapat (başarı sonrası ekranda kalmasın).
        self._dismiss_toast(getattr(self, "_net_cancel_toast", None))
        self._net_cancel_toast = None
        self._dismiss_toast(getattr(self, "_download_complete_toast", None))
        self._download_complete_toast = None

        def _open():
            self._reveal_last_download()
            self._dismiss_toast(self._download_complete_toast)
            self._download_complete_toast = None

        # timeout_s=0 => otomatik kapanmaz (libadwaita).
        self._download_complete_toast = self.show_toast_action(
            "İndirme tamamlandı",
            button_label="Göster",
            on_click=_open,
            timeout_s=0,
        )

    def _download_active(self) -> bool:
        return ("download" in getattr(self, "_busy_reasons", set()))

    def _clear_result_toasts_for_new_action(self, *, clear_download_complete: bool = True):
        """Yeni format tarama / indirme başlarken, önceki (kalıcı) toast'ları kapat.

        clear_download_complete=False => 'İndirme tamamlandı / Göster' toast'ını korur.
        (Örn. aynı linke tekrar format taraması yapılınca gereksiz yere kaybolmaması için.)
        """
        if clear_download_complete:
            self._dismiss_toast(getattr(self, "_download_complete_toast", None))
            self._download_complete_toast = None
            self._last_download_toast_url = None

        self._dismiss_toast(getattr(self, "_net_cancel_toast", None))
        self._net_cancel_toast = None
        self._dismiss_toast(getattr(self, "_net_down_toast", None))
        self._net_down_toast = None
        self._net_down_toast_shown = False

        if getattr(self, "_net_check_timer_id", 0):
            try:
                _safe_source_remove(self._net_check_timer_id)
            except Exception:
                pass
            self._net_check_timer_id = 0
        self._net_check_escalated = False

    def _compute_net_available(self) -> bool:
        """NetworkMonitor'dan 'gerçek internet' var mı kararını üretir.
        Not: get_network_available() her zaman yeterli değil; connectivity NONE/LIMITED ise offline sayıyoruz.
        """
        mon = getattr(self, "_netmon", None)
        if mon is None:
            return True
        try:
            avail = bool(mon.get_network_available())
        except Exception:
            avail = True
        try:
            conn = mon.get_connectivity()
            # NONE ve LIMITED durumlarında yt-dlp büyük ihtimalle ilerleyemez (DNS/portal vb.)
            if conn in (Gio.NetworkConnectivity.NONE, Gio.NetworkConnectivity.LIMITED):
                return False
        except Exception:
            pass
        return bool(avail)

    def _on_net_connectivity_notify(self, _monitor, _pspec):
        """notify::connectivity sinyali geldiğinde network-changed gibi davran."""
        available = self._compute_net_available()
        # _on_network_changed imzası (monitor, available)
        self._on_network_changed(getattr(self, "_netmon", None), available)
    def _schedule_net_down_toast(self):
        """Schedule the persistent network warning without back-to-back toasts.

        We intentionally *do not* show an intermediate "İnternet Bağlantısı Kesildi" toast (2s),
        because we later show a more actionable warning. Instead we only show a single persistent
        toast after the link has been down continuously for >=3s during (or right after) a download:
        "Lütfen İnternet Bağlantınızı Kontrol Edin".
        """
        # Stop any legacy 2s timer if present
        _tmp_id = getattr(self, "_net_down_timer_id", 0)
        self._net_down_timer_id = 0
        _safe_source_remove(_tmp_id)

        # Start the 3s check timer (deterministic) if not already running
        self._schedule_net_check_toast()
        return False

    def _schedule_net_check_toast(self):
        """If the connection stays down for >=3.0s during an active download, show an extra warning.

        Adw.ToastOverlay effectively shows one persistent toast at a time; so instead of adding a
        second persistent toast, we *escalate* the existing net-down toast title.
        """
        if getattr(self, "_net_check_timer_id", 0):
            return
        self._net_check_started_us = GLib.get_monotonic_time()
        self._net_check_timer_id = GLib.timeout_add(50, self._net_check_timeout)

    def _net_check_timeout(self):
        available = self._compute_net_available()
        if available:
            self._net_check_timer_id = 0
            return False

        started_us = int(getattr(self, "_net_check_started_us", 0))
        elapsed_s = (GLib.get_monotonic_time() - started_us) / 1_000_000.0 if started_us else 999.0
        if elapsed_s < 3.0:
            return True

        self._net_check_timer_id = 0

        if getattr(self, "_net_check_escalated", False):
            return False

        if self._download_active() or getattr(self, "_net_was_down_during_download", False):
            toast = getattr(self, "_net_down_toast", None)
            if toast is None:
                toast = Adw.Toast.new("Lütfen İnternet Bağlantınızı Kontrol Edin")
                toast.set_timeout(int(getattr(self, "_persist_toast_timeout_s", 86400)))
                toast.set_priority(Adw.ToastPriority.HIGH)
                self.toast_overlay.add_toast(toast)
                self._net_down_toast = toast
            else:
                try:
                    toast.set_title("Lütfen İnternet Bağlantınızı Kontrol Edin")
                except Exception:
                    self._dismiss_toast(toast)
                    t2 = Adw.Toast.new("Lütfen İnternet Bağlantınızı Kontrol Edin")
                    t2.set_timeout(int(getattr(self, "_persist_toast_timeout_s", 86400)))
                    t2.set_priority(Adw.ToastPriority.HIGH)
                    self.toast_overlay.add_toast(t2)
                    self._net_down_toast = t2

            self._net_check_escalated = True

        return False

    def _on_network_changed(self, _monitor, available: bool):
        # Bağlantı değişti
        self._net_available = bool(available)
        # Sağlamlaştır: bazı durumlarda 'available' eksik kalabiliyor.
        self._net_available = self._compute_net_available() if getattr(self, "_netmon", None) is not None else self._net_available

        if self._net_available:
            # Timer'ı kapat
            _tmp_id = getattr(self, "_net_down_timer_id", 0)
            self._net_down_timer_id = 0
            _safe_source_remove(_tmp_id)
            # Net-check timer'ını kapat
            _tmp_id = getattr(self, "_net_check_timer_id", 0)
            self._net_check_timer_id = 0
            _safe_source_remove(_tmp_id)
            self._net_check_escalated = False


            # Ağ uyarı toast'ını kapat
            self._dismiss_toast(getattr(self, "_net_down_toast", None))
            self._net_down_toast = None
            self._net_down_toast_shown = False

            # Eğer indirme ağ kopması yüzünden iptal olduysa, bağlantı gelince kullanıcıya söyle
            if getattr(self, "_download_failed_due_to_net", False):
                # Eski iptal toast'ı varsa kapatıp yenisini göster
                self._dismiss_toast(getattr(self, "_net_cancel_toast", None))
                self._net_cancel_toast = None

                try:
                    toast = Adw.Toast.new("İndirme işlemi, internet bağlantısı kesildiği için İPTAL OLDU!")
                    toast.set_timeout(int(getattr(self, '_persist_toast_timeout_s', 86400)))
                    if hasattr(toast, "set_priority"):
                        try:
                            toast.set_priority(Adw.ToastPriority.HIGH)
                        except Exception:
                            pass
                    self.toast_overlay.add_toast(toast)
                    self._net_cancel_toast = toast
                except Exception:
                    pass

                # Bu toast'ı yeni aksiyona kadar tutacağız; flag'i tekrar tetiklememek için sıfırla
                self._download_failed_due_to_net = False
                self._net_was_down_during_download = False

            return

        # Bağlantı koptu: download aktifse (veya kopma yüzünden yeni iptal olduysa) 2 sn sonra uyar
        if self._download_active():
            self._net_was_down_during_download = True
        if (self._download_active() or getattr(self, "_net_was_down_during_download", False)) and (not getattr(self, "_net_down_toast_shown", False)):
            GLib.idle_add(self._schedule_net_down_toast)

    def _net_down_timeout(self):
        # Legacy (kept for safety): previously used for the 2s "İnternet Bağlantısı Kesildi" toast.
        # We no longer use this path to avoid back-to-back warnings.
        self._net_down_timer_id = 0
        return False

    def _is_network_error_message(self, msg: str) -> bool:
        s = (msg or "").lower()
        # yt-dlp / urllib / socket kaynaklı tipik ağ hataları
        patterns = [
            "name or service not known",
            "temporary failure in name resolution",
            "failed to resolve",
            "getaddrinfo",
            "dns",
            "network is unreachable",
            "no route to host",
            "connection refused",
            "connection reset",
            "connection aborted",
            "timed out",
            "timeout",
            "errno -2",
            "[errno -2]",
        ]
        if any(p in s for p in patterns):
            return True
        # "Giving up after N retries" genelde ağ kopması / DNS için görülür; tek başına yeterli olmasın diye birlikte kontrol ediyoruz.
        if ("giving up after" in s and "retries" in s) and ("download" in s or "error" in s):
            return True
        return False
    def _reveal_last_download(self, *_args):
        # İdeal: indirilen dosyayı dosya yöneticisinde seçerek göster.
        # Eğer yol yoksa/boşsa, en azından indirme klasörünü aç.
        p = (self.last_download_path or "").strip()
        if not p:
            try:
                self._open_folder_fallback(self.output_dir)
            except Exception:
                self.set_status("warn", "Gösterilecek dosya yok", toast=True)
            return
        self._reveal_in_file_manager(p)

    def _reveal_in_file_manager(self, path: str):
        try:
            f = Gio.File.new_for_path(path)
            if not f.query_exists(None):
                self.set_status("warn", "Dosya bulunamadı", toast=True)
                return

            uri = f.get_uri()

            # En iyi ihtimal: dosyayı dosya yöneticisinde seçerek göster (org.freedesktop.FileManager1)
            try:
                proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SESSION,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    "org.freedesktop.FileManager1",
                    "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1",
                    None,
                )
                proxy.call_sync(
                    "ShowItems",
                    GLib.Variant("(ass)", ([uri], "")),
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
                return
            except Exception:
                pass

            # Fallback: klasörü aç (seçim garantisi yok)
            parent = f.get_parent()
            if parent is not None:
                Gio.AppInfo.launch_default_for_uri(parent.get_uri(), None)
            else:
                Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as e:
            self.set_status("error", f"Dosya yöneticisi açılamadı: {e}", toast=True)
    def _set_format_model(self, names: list[str], selected_index: int = 0):
        n = self.format_model.get_n_items()
        if n:
            self.format_model.splice(0, n, [])
        for s in names:
            self.format_model.append(s)

        if selected_index < 0 or selected_index >= len(names):
            selected_index = 0

        # Bu seçim "varsayılan/AUTO" seçimdir (kullanıcı henüz el ile seçmedi)
        self._auto_default_index = selected_index
        self._user_picked_format = False

        # set_selected() notify::selected tetikler; bunu "user picked" saymıyoruz.
        self._setting_selected_programmatically = True
        try:
            self.format_row.set_selected(selected_index)
        finally:
            self._setting_selected_programmatically = False

    def _on_format_selected_changed(self, row, pspec):
        if self._setting_selected_programmatically:
            return
        self._user_picked_format = True
        try:
            self.format_row.queue_draw()
        except Exception:
            pass

    # ---------- ComboRow item factory (modern ikon + premium badge) ----------
    def _icon_for_format_key(self, key: str) -> str:
        if not key:
            return "dialog-information-symbolic"

        if key in ("video_best", "video_2160p", "video_1440p", "video_1080p"):
            return "camera-video-symbolic"

        if key.startswith("audio_"):
            return "audio-x-generic-symbolic"

        if key.startswith("video_only_"):
            return "video-x-generic-symbolic"

        return "dialog-information-symbolic"

    def _badge_for_format_key(self, key: str) -> tuple[str | None, str | None]:
        if not key:
            return (None, None)

        # Video kalite
        if key == "video_2160p":
            return ("4K", "badge")
        if key == "video_1440p":
            return ("2K", "badge")
        if key == "video_1080p":
            return ("1080p", "badge")

        # Audio
        if key == "audio_opus":
            return ("OPUS", "badge-audio")
        if key == "audio_m4a":
            return ("M4A", "badge-audio")

        # Video-only container
        if key == "video_only_mkv_1080":
            return ("MKV", "badge-container")
        if key == "video_only_mp4_1080":
            return ("MP4", "badge-container")

        # video_best (listede kaliteye çevrilecek, display’de AUTO override edilebilir)
        if key == "video_best":
            p = int((self.last_caps or {}).get("max_height") or 0)
            p = self._to_std_p(p)
            if p >= 2160:
                return ("4K", "badge")
            if p >= 1440:
                return ("2K", "badge")
            if p >= 1080:
                return ("1080p", "badge")
            if p >= 720:
                return ("720p", "badge")
            if p > 0:
                return (f"{p}p", "badge")
            return ("AUTO", "badge")

        return (None, None)

    def _badge_for_video_best_in_list(self) -> tuple[str | None, str | None]:
        p = int((self.last_caps or {}).get("max_height") or 0)
        p = self._to_std_p(p)
        if p >= 2160:
            return ("4K", "badge")
        if p >= 1440:
            return ("2K", "badge")
        if p >= 1080:
            return ("1080p", "badge")
        if p >= 720:
            return ("720p", "badge")
        if p > 0:
            return (f"{p}p", "badge")
        return (None, None)

    def _setup_format_item_factory(self):
        # Kapalı satır (display)
        factory_display = Gtk.SignalListItemFactory()
        factory_display.connect("setup", self._on_format_factory_setup)
        factory_display.connect("bind", self._on_format_factory_bind_display)
        self.format_row.set_factory(factory_display)

        # Popover listesi
        factory_list = Gtk.SignalListItemFactory()
        factory_list.connect("setup", self._on_format_factory_setup)
        factory_list.connect("bind", self._on_format_factory_bind_list)
        try:
            self.format_row.set_list_factory(factory_list)
        except Exception:
            pass

    def _on_format_factory_setup(self, factory, list_item: Gtk.ListItem):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(10)
        row.set_margin_end(10)

        icon = Gtk.Image()
        icon.set_pixel_size(16)
        icon.set_valign(Gtk.Align.CENTER)

        label = Gtk.Label(xalign=0)
        label.set_hexpand(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)

        badge = Gtk.Label()
        badge.set_valign(Gtk.Align.CENTER)
        badge.set_visible(False)
        badge.add_css_class("badge")

        row.append(icon)
        row.append(label)
        row.append(badge)

        row._icon = icon
        row._label = label
        row._badge = badge
        list_item.set_child(row)

    def _apply_badge(self, badge_widget: Gtk.Label, badge_text: str | None, badge_class: str | None):
        for c in ("badge", "badge-audio", "badge-container"):
            badge_widget.remove_css_class(c)

        if badge_text:
            badge_widget.set_text(badge_text)
            badge_widget.set_visible(True)
            badge_widget.add_css_class(badge_class or "badge")
        else:
            badge_widget.set_visible(False)

    def _on_format_factory_bind_display(self, factory, list_item: Gtk.ListItem):
        row = list_item.get_child()
        if row is None:
            return

        item = list_item.get_item()
        try:
            text = item.get_string()
        except Exception:
            text = str(item) if item is not None else ""

        sel = -1
        try:
            sel = self.format_row.get_selected()
        except Exception:
            sel = -1

        key = ""
        if 0 <= sel < len(getattr(self, "available_format_keys", [])):
            key = self.available_format_keys[sel]

        row._label.set_text(text)
        row._icon.set_from_icon_name(self._icon_for_format_key(key))

        # Varsayılan (kullanıcı seçmedi) durumda sadece display’de AUTO
        if (
            self._auto_default_index is not None
            and not self._user_picked_format
            and sel == self._auto_default_index
        ):
            self._apply_badge(row._badge, "AUTO", "badge")
        else:
            badge_text, badge_class = self._badge_for_format_key(key)
            self._apply_badge(row._badge, badge_text, badge_class)

    def _on_format_factory_bind_list(self, factory, list_item: Gtk.ListItem):
        row = list_item.get_child()
        if row is None:
            return

        item = list_item.get_item()
        try:
            text = item.get_string()
        except Exception:
            text = str(item) if item is not None else ""

        try:
            pos = list_item.get_position()
        except Exception:
            pos = -1

        key = ""
        if 0 <= pos < len(getattr(self, "available_format_keys", [])):
            key = self.available_format_keys[pos]

        row._label.set_text(text)
        row._icon.set_from_icon_name(self._icon_for_format_key(key))

        if key == "video_best":
            badge_text, badge_class = self._badge_for_video_best_in_list()
            self._apply_badge(row._badge, badge_text, badge_class)
        else:
            badge_text, badge_class = self._badge_for_format_key(key)
            self._apply_badge(row._badge, badge_text, badge_class)

    # ---------- Folder picker ----------
    def on_select_folder(self, button):
        dialog = Gtk.FileDialog(title="İndirme klasörünü seç")
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            path = folder.get_path()
            if path:
                self.output_dir = path
                self.folder_label.set_text(path)
                self.set_status("ok", "Klasör seçildi", toast=True)
        except GLib.Error:
            pass

    # ---------- Format scanning helpers ----------
    def _to_std_p(self, p: int) -> int:
        if p <= 0:
            return 0
        for std in _STD_P:
            if abs(p - std) <= 12:
                return std
        return p

    def _is_sr_upscaled(self, f: dict) -> bool:
        fid = str(f.get("format_id") or "").lower()
        note = str(f.get("format_note") or "").lower()
        fmt = str(f.get("format") or "").lower()
        if "sr" in fid:
            return True
        if "ai-upscaled" in note or "upscaled" in note:
            return True
        if "ai-upscaled" in fmt or "upscaled" in fmt:
            return True
        return False

    def _extract_height(self, f: dict) -> int:
        h = f.get("height")
        if isinstance(h, int) and h > 0:
            return h

        res = (f.get("resolution") or "").strip()
        m = re.search(r"x(\d{3,4})$", res)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

        note = (f.get("format_note") or "").strip()
        m = re.search(r"(\d{3,4})p", note)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

        fmt = (f.get("format") or "").strip()
        m = re.search(r"(\d{3,4})p", fmt)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

        return 0
    def _detect_capabilities(self, formats: list[dict]) -> dict:
        video_streams_all: list[dict] = []
        video_only_nosr: list[dict] = []
        audio_only: list[dict] = []

        has_vp9_2160 = False
        has_vp9_1440 = False
        has_vp9_1080 = False
        has_mp4_h264_1080 = False

        max_h_nosr = 0
        max_h_any_nosr = 0

        for f in formats:
            if not isinstance(f, dict):
                continue

            vcodec = (f.get("vcodec") or "").lower()
            acodec = (f.get("acodec") or "").lower()
            ext = (f.get("ext") or "").lower()

            has_video = bool(vcodec and vcodec != "none")
            has_audio = bool(acodec and acodec != "none")

            if has_video:
                video_streams_all.append(f)
                # Video (muxed dahil): premium/SR upscaled stream'leri max_height hesabına katma
                if not self._is_sr_upscaled(f):
                    h_any = self._to_std_p(self._extract_height(f))
                    if h_any > max_h_any_nosr:
                        max_h_any_nosr = h_any

            if has_audio and not has_video:
                audio_only.append(f)

            if has_video and not has_audio:
                # video-only
                if self._is_sr_upscaled(f):
                    continue

                h = self._to_std_p(self._extract_height(f))
                if h > max_h_nosr:
                    max_h_nosr = h

                video_only_nosr.append(f)

                if "vp9" in vcodec or vcodec.startswith("vp09"):
                    if h >= 2150:
                        has_vp9_2160 = True
                    elif h >= 1430:
                        has_vp9_1440 = True
                    elif h >= 1070:
                        has_vp9_1080 = True

                if ext == "mp4" and (("avc1" in vcodec) or ("h264" in vcodec)):
                    if h >= 1070:
                        has_mp4_h264_1080 = True

        has_opus_audio = any(((f.get("acodec") or "").lower() == "opus") for f in audio_only)
        has_m4a_audio = any(
            ((f.get("ext") or "").lower() == "m4a") or ("mp4a" in ((f.get("acodec") or "").lower()))
            for f in audio_only
        )

        return {
            "video_count": len(video_streams_all),
            "audio_count": len(audio_only),
            "max_height": int(max_h_any_nosr or max_h_nosr or 0),
            "has_opus_audio": bool(has_opus_audio),
            "has_m4a_audio": bool(has_m4a_audio),
            "has_vp9_2160": bool(has_vp9_2160),
            "has_vp9_1440": bool(has_vp9_1440),
            "has_vp9_1080": bool(has_vp9_1080),
            "has_mp4_h264_1080": bool(has_mp4_h264_1080),
        }

    def _build_video_best_override(self, formats: list[dict], caps: dict) -> str | None:
        """Video+Ses için en iyi format_id kombinasyonunu üretir (SR/premium upscaled hariç).

        Tercih sırası:
        1) Video-only + Audio-only (Opus > M4A > diğer)
        2) Muxed (video+audio) tek format_id (fallback)
        """
        if not formats:
            return None

        def _is_video(f: dict) -> bool:
            v = (f.get("vcodec") or "").lower()
            return bool(v and v != "none")

        def _is_audio(f: dict) -> bool:
            a = (f.get("acodec") or "").lower()
            return bool(a and a != "none")

        def _height(f: dict) -> int:
            return self._to_std_p(self._extract_height(f))

        def _vcodec_rank(vcodec: str) -> int:
            v = (vcodec or "").lower()
            if "vp9" in v or v.startswith("vp09"):
                return 3
            if "av01" in v or v.startswith("av01"):
                return 2
            if "avc1" in v or "h264" in v:
                return 1
            return 0

        def _audio_rank(f: dict) -> int:
            a = (f.get("acodec") or "").lower()
            ext = (f.get("ext") or "").lower()
            if a == "opus":
                return 3
            if ext == "m4a" or "mp4a" in a:
                return 2
            if a and a != "none":
                return 1
            return 0

        video_only: list[tuple[tuple, dict]] = []
        muxed: list[tuple[tuple, dict]] = []
        audio_only: list[tuple[tuple, dict]] = []

        for f in formats:
            if not isinstance(f, dict):
                continue
            if self._is_sr_upscaled(f):
                continue

            fid = f.get("format_id")
            if not fid:
                continue

            has_v = _is_video(f)
            has_a = _is_audio(f)

            if has_a and (not has_v):
                # audio-only adayları
                r = _audio_rank(f)
                abr = float(f.get("abr") or 0.0)
                tbr = float(f.get("tbr") or 0.0)
                audio_only.append(((r, abr, tbr), f))
                continue

            if not has_v:
                continue

            h = _height(f)
            if h <= 0:
                continue

            v_rank = _vcodec_rank(f.get("vcodec") or "")
            tbr = float(f.get("tbr") or 0.0)
            fps = float(f.get("fps") or 0.0)
            # Öncelik: çözünürlük > codec tercihi > bitrate > fps
            score = (h, v_rank, tbr, fps)

            if has_a:
                muxed.append((score, f))
            else:
                video_only.append((score, f))

        # 1) Video-only tercih
        chosen_video: dict | None = None
        if video_only:
            chosen_video = max(video_only, key=lambda x: x[0])[1]
        elif muxed:
            chosen_video = max(muxed, key=lambda x: x[0])[1]

        if not chosen_video:
            return None

        # Eğer seçtiğimiz video zaten muxed ise tek format_id yeterli
        if _is_audio(chosen_video):
            return str(chosen_video.get("format_id"))

        # Video-only ise uygun audio-only seç
        if not audio_only:
            return str(chosen_video.get("format_id"))

        chosen_audio = max(audio_only, key=lambda x: x[0])[1]
        return f"{chosen_video.get('format_id')}+{chosen_audio.get('format_id')}"

    def _available_keys_from_caps(self, caps: dict) -> list[str]:
        avail: list[str] = []

        has_opus = bool(caps.get("has_opus_audio"))
        has_m4a = bool(caps.get("has_m4a_audio"))

        vp9_2160 = bool(caps.get("has_vp9_2160"))
        vp9_1440 = bool(caps.get("has_vp9_1440"))
        vp9_1080 = bool(caps.get("has_vp9_1080"))
        mp4_h264_1080 = bool(caps.get("has_mp4_h264_1080"))

        # 4K/2K/1080p = VP9 video + en iyi Opus ses, çıktı MKV
        if has_opus:
            if vp9_2160 and "video_2160p" in FORMAT_OPTIONS:
                avail.append("video_2160p")
            if vp9_1440 and "video_1440p" in FORMAT_OPTIONS:
                avail.append("video_1440p")
            if vp9_1080 and "video_1080p" in FORMAT_OPTIONS:
                avail.append("video_1080p")

        # Ses seçenekleri
        if has_opus and "audio_opus" in FORMAT_OPTIONS:
            avail.append("audio_opus")
        if has_m4a and "audio_m4a" in FORMAT_OPTIONS:
            avail.append("audio_m4a")

        # Sadece video (1080p)
        if vp9_1080 and "video_only_mkv_1080" in FORMAT_OPTIONS:
            avail.append("video_only_mkv_1080")
        if mp4_h264_1080 and "video_only_mp4_1080" in FORMAT_OPTIONS:
            avail.append("video_only_mp4_1080")

        return avail


    def _policy_reason_from_caps(self, caps: dict) -> str:
        """Politika nedeniyle uygun seçenek üretemediğimizde kullanıcıya açıklama üretir."""
        video_count = int(caps.get("video_count") or 0)
        audio_count = int(caps.get("audio_count") or 0)

        has_opus = bool(caps.get("has_opus_audio"))
        has_m4a = bool(caps.get("has_m4a_audio"))

        vp9_2160 = bool(caps.get("has_vp9_2160"))
        vp9_1440 = bool(caps.get("has_vp9_1440"))
        vp9_1080 = bool(caps.get("has_vp9_1080"))
        mp4_h264_1080 = bool(caps.get("has_mp4_h264_1080"))

        # En temel yokluklar
        if video_count <= 0 and audio_count <= 0:
            return "Uygun format bulunamadı: Bu URL'de indirilebilir medya akışı tespit edilemedi."
        if not has_opus and not has_m4a:
            return "Politika uygun değil: Bu içerikte Opus veya M4A ses akışı yok."
        if video_count > 0 and not (vp9_2160 or vp9_1440 or vp9_1080 or mp4_h264_1080):
            return "Politika uygun değil: VP9 (2160/1440/1080) veya MP4 H.264 (1080) video akışı yok."

        # Daha ayrıntılı ama kısa
        missing = []
        if video_count > 0:
            if not (vp9_2160 or vp9_1440 or vp9_1080):
                missing.append("VP9 video (2160/1440/1080)")
            if not mp4_h264_1080:
                missing.append("MP4 H.264 video-only (1080)")
        if not has_opus:
            missing.append("Opus ses")
        if not has_m4a:
            missing.append("M4A ses")

        if missing:
            return "Politika uygun değil: Eksik olanlar: " + ", ".join(missing)
        return "Uygun format bulunamadı."

    def _display_name_for_key(self, key: str, caps: dict | None) -> str:
        if key == "video_best":
            return "Video + Ses (MKV)"

        try:
            return FORMAT_OPTIONS[key]["name"]
        except Exception:
            return key

    # ---------- Scan ----------
    
    def on_paste_clicked(self, *_args):
        """Panodaki son metni URL alanına yapıştır."""
        try:
            display = Gdk.Display.get_default()
            if not display:
                return
            cb = display.get_clipboard()
            cb.read_text_async(None, self._on_clipboard_text_ready, None)
        except Exception:
            # Clipboard okunamazsa sessizce geç
            return

    def _on_clipboard_text_ready(self, clipboard, result, _user_data):
        try:
            text = clipboard.read_text_finish(result)
        except Exception:
            text = None
        if not text:
            return
        text = text.strip()
        if not text:
            return
        self.url_entry.set_text(text)
        # Pano içeriği URL ise bunu aktif URL olarak kaydet ve format listesini sıfırla
        if text.startswith("http://") or text.startswith("https://"):
            self.current_url = text
            self.current_title = ""
            self.available_format_keys = []
            self.last_scanned_url = ""
            self._set_format_model(["(Önce 'Format Tara')"], 0)
            self.format_row.set_sensitive(False)
            self.download_button.set_sensitive(False)
            self._set_download_ready(False)
        try:
            # Butona tıklayınca odak düğmeye geçer; sonra girişe odak alınca GTK bazen tüm metni seçer.
            # Fokus değişimini aynı frame içinde zorlamamak için işlemi idle'a alıyoruz.
            def _refocus_and_clear():
                try:
                    self.url_entry.grab_focus()
                    self.url_entry.set_position(-1)
                    self.url_entry.select_region(-1, -1)
                    self.url_entry.set_position(-1)
                except Exception:
                    pass
                return False
            GLib.idle_add(_refocus_and_clear)
        except Exception:
            pass

    def _on_url_entry_changed(self, entry):
        # Kullanıcı yeni bir URL yazarsa mevcut format listesini geçersiz say.
        text = entry.get_text().strip()
        if text.startswith("http://") or text.startswith("https://"):
            if text != self.current_url:
                self.current_url = text
                self.current_title = ""
                self._set_last_download_path(None)
                # Playlist state'i temizle
                self._playlist_meta = {}
                self._playlist_scan_item = None
                try:
                    self._update_playlist_meta_ui(None)
                except Exception:
                    pass
                self.available_format_keys = []
                self.last_scanned_url = ""
                self._set_format_model(["(Önce 'Format Tara')"], 0)
                self.format_row.set_sensitive(False)
                self.download_button.set_sensitive(False)
                self._set_download_ready(False)
                try:
                    self.url_entry.set_tooltip_text(text)
                except Exception:
                    pass

    def _set_scanned_title(self, title: str, url: str):
        # Format taraması bitince entry içinde başlığı göster; gerçek URL'yi state'te sakla.
        self.current_url = url or self.current_url
        self.current_title = title or ""
        show = (title or "").strip() or url
        self.url_entry.set_text(show)
        try:
            self.url_entry.set_tooltip_text(url)
        except Exception:
            pass



    def _on_playlist_toggle_changed(self, *_args):
        """Playlist toggle değişince playlist-items alanını etkinleştir."""
        try:
            active = bool(self.playlist_switch.get_active())
            self.playlist_items_entry.set_sensitive(active)
        except Exception:
            pass

    def _update_playlist_meta_ui(self, meta: dict | None):
        """UI'da playlist bilgisini güncelle (GLib.idle_add ile çağrılabilir)."""
        self._playlist_meta = meta or {}
        try:
            if not meta or not meta.get("is_playlist"):
                self.playlist_info_row.set_sensitive(False)
                self.playlist_info_row.set_subtitle("—")
                return False

            title = (meta.get("title") or "").strip() or "Playlist"
            count = meta.get("count") or 0
            extra = ""
            scan_item = getattr(self, "_playlist_scan_item", None)
            if isinstance(scan_item, int) and scan_item > 0:
                extra = f" • Tarama öğesi: {scan_item}"

            self.playlist_info_row.set_sensitive(True)
            self.playlist_info_row.set_subtitle(f"{title} • {count} öğe{extra}")
        except Exception:
            # UI güncellemesi best-effort
            pass
        return False


    def on_scan_formats_clicked(self, button):
        entered = self.url_entry.get_text().strip()
        if entered.startswith("http://") or entered.startswith("https://"):
            url = entered
            self.current_url = url
        else:
            url = self.current_url.strip()

            # Playlist öneri toast'ı: farklı URL taranırsa tekrar önerilebilsin
            last_any = (getattr(self, "_last_scanned_url_any", "") or "").strip()
            if url.strip() and last_any and (url.strip() != last_any):
                self._playlist_suggested_url = ""
            self._last_scanned_url_any = url.strip()


        # Yeni tarama başlarken: sonuç/toast temizliği
        # - Ağ toast'ları her zaman temizlensin
        # - İndirme tamamlandı toast'ı sadece farklı link taranıyorsa temizlensin
        last_toast_url = (getattr(self, "_last_download_toast_url", None) or "").strip()
        clear_dl_toast = bool(last_toast_url) and (url.strip() != last_toast_url)
        self._clear_result_toasts_for_new_action(clear_download_complete=clear_dl_toast)

        if not url:
            self.set_status("warn", "URL girilmedi", toast=True)
            return

        if url == self.last_scanned_url and self.available_format_keys:
            self.set_status("ok", "Formatlar hazır", toast=True)
            self.format_row.set_sensitive(True)
            self.download_button.set_sensitive(True)
            self._set_download_ready(True)
            return

        # Format taraması başlarken indirme progress bar'ını sıfırla
        try:
            self.progress.set_fraction(0.0)
            self.progress.set_text("%0")
            self.progress.set_show_text(True)
            self._last_speed_mbps = None
            self._last_eta = None
        except Exception:
            pass

        self._busy_push("scan")
        self.scan_button.set_sensitive(False)
        self.format_row.set_sensitive(False)
        self.download_button.set_sensitive(False)
        self._set_download_ready(False)
        self.set_status("download", "Formatlar taranıyor...")

        playlist_on = bool(getattr(self, 'playlist_switch', None) and self.playlist_switch.get_active())
        playlist_items_spec = ''
        try:
            playlist_items_spec = (self.playlist_items_entry.get_text() or '').strip()
        except Exception:
            playlist_items_spec = ''

        def worker(u: str, playlist_on: bool, playlist_items_spec: str):
            try:
                # Playlist probe (yalnızca gerekli olduğunda)
                meta = None
                maybe_playlist = (("list=" in u) or ("/playlist" in u))
                if maybe_playlist or playlist_on:
                    try:
                        meta = probe_playlist(u)
                    except Exception:
                        meta = None

                GLib.idle_add(self._update_playlist_meta_ui, meta)

                # Playlist tespit edildi ama kullanıcı playlist modunu açmadıysa: öneri toast'ı
                suggest_playlist = bool(meta and meta.get("is_playlist") and (not meta.get("playlist_only")) and (not playlist_on))
                if suggest_playlist:
                    GLib.idle_add(self._maybe_show_playlist_suggestion, u, meta)

                # Playlist linki ama kullanıcı playlist modunu açmadıysa: uyarı ver ve taramayı durdur.
                if meta and meta.get("is_playlist") and meta.get("playlist_only") and (not playlist_on):
                    GLib.idle_add(
                        self.set_status,
                        "warn",
                        "Bu URL bir playlist. 'Gelişmiş > Playlist indir' seçeneğini açıp tekrar 'Format Tara' yapın.",
                        True,
                    )
                    GLib.idle_add(self._set_format_model, ["(Playlist için 'Playlist indir' açılmalı)"], 0)
                    GLib.idle_add(self.format_row.set_sensitive, False)
                    GLib.idle_add(self.download_button.set_sensitive, False)
                    GLib.idle_add(self._set_download_ready, False)
                    return

                # Playlist modunda format taraması:
                # - playlist-items doluysa: deterministik olarak ilk index'ten tarar
                # - boşsa: ilk N öğe içinde en yüksek çözünürlüğü yakalayan öğeden tarar
                if meta and meta.get("is_playlist") and playlist_on:
                    pl_title = (meta.get("title") or "").strip()
                    pl_count = int(meta.get("count") or 0)

                    if playlist_items_spec:
                        scan_item = first_index_from_playlist_items_spec(playlist_items_spec)
                        self._playlist_scan_item = scan_item
                        formats, item_title = get_formats_for_playlist_item(u, scan_item)
                        title = pl_title or item_title
                    else:
                        scan_item = 1
                        self._playlist_scan_item = scan_item
                        formats, item_title = get_formats_for_playlist_item(u, scan_item)
                        title = pl_title or item_title

                    GLib.idle_add(self._update_playlist_meta_ui, meta)
                else:
                    formats, title = get_formats(u)
                caps = self._detect_capabilities(formats)
                self.last_caps = caps
                self._format_overrides = {}

                avail = self._available_keys_from_caps(caps)

                # Eğer politika nedeniyle (örn. 1080p+ VP9 yok / sadece premium SR var) video seçeneği çıkmıyorsa,
                # 'Video + Ses (MKV)' (video_best) için güvenli bir format_id kombinasyonu üretip listeye ekleyelim.
                has_video_av = any(k in avail for k in ("video_2160p", "video_1440p", "video_1080p"))
                if (not has_video_av) and (caps.get("video_count") or 0) > 0 and (caps.get("audio_count") or 0) > 0:
                    best_spec = self._build_video_best_override(formats, caps)
                    if best_spec and ("video_best" in FORMAT_OPTIONS):
                        self._format_overrides["video_best"] = best_spec
                        if "video_best" not in avail:
                            avail.insert(0, "video_best")
                if not avail:
                    reason = self._policy_reason_from_caps(caps)
                    GLib.idle_add(self.set_status, "warn", reason, True)
                    GLib.idle_add(self._set_format_model, ["(Uygun format yok)"], 0)
                    GLib.idle_add(self.format_row.set_sensitive, False)
                    GLib.idle_add(self.download_button.set_sensitive, False)
                    GLib.idle_add(self._set_download_ready, False)
                    return

                self.available_format_keys = avail
                self.last_scanned_url = u

                preferred_index = 0
                if "audio_opus" in avail:
                    preferred_index = avail.index("audio_opus")
                elif "audio_m4a" in avail:
                    preferred_index = avail.index("audio_m4a")

                names = [self._display_name_for_key(k, caps) for k in avail]

                GLib.idle_add(self._set_format_model, names, preferred_index)
                GLib.idle_add(self.format_row.set_sensitive, True)
                GLib.idle_add(self.download_button.set_sensitive, True)
                GLib.idle_add(self._set_download_ready, True)
                GLib.idle_add(self._set_scanned_title, title, u)
                GLib.idle_add(self.set_status, "ok", "Formatlar hazır", (not suggest_playlist))

            except Exception as e:
                GLib.idle_add(self.set_status, "error", f"Format tarama hatası: {e}", True)
            finally:
                GLib.idle_add(self.scan_button.set_sensitive, True)
                GLib.idle_add(self._busy_pop, "scan")

        run_in_thread(worker, url, playlist_on, playlist_items_spec)
    def on_cancel_clicked(self, button):
        if self.cancel_event is not None:
            # İptalden sonra gelebilecek gecikmeli progress güncellemelerini yok say
            self._ignore_progress_updates = True
            self._clear_progress_text()

            self.cancel_event.set()
            self.cancel_button.set_sensitive(False)
            self.set_status("cancel", "İptal istendi, durduruluyor...", toast=True)

    # ---------- Download ----------

    def on_download_clicked(self, button):
        self._clear_result_toasts_for_new_action()
        entered = self.url_entry.get_text().strip()
        if entered.startswith("http://") or entered.startswith("https://"):
            url = entered
            self.current_url = url
        else:
            url = self.current_url.strip()

        if not url:
            self.set_status("warn", "URL girilmedi", toast=True)
            return

        if not self.available_format_keys:
            self.set_status("warn", "Önce 'Format Tara' yap.", toast=True)
            return

        idx = self.format_row.get_selected()
        if idx < 0 or idx >= len(self.available_format_keys):
            self.set_status("warn", "Format seçimi geçersiz.", toast=True)
            return

        format_key = self.available_format_keys[idx]

        # Playlist indirme durumu (2/5) hazırlığı
        playlist_mode = bool(getattr(self, '_playlist_meta', {}).get('is_playlist')) and bool(getattr(self, 'playlist_switch', None) and self.playlist_switch.get_active())
        playlist_items_spec = ''
        try:
            playlist_items_spec = (self.playlist_items_entry.get_text() or '').strip() if playlist_mode else ''
        except Exception:
            playlist_items_spec = ''
        selected_total = 0
        if playlist_mode:
            if playlist_items_spec:
                selected_total = self._count_playlist_items_spec(playlist_items_spec)
            if not selected_total:
                try:
                    selected_total = int(getattr(self, '_playlist_meta', {}).get('count') or 0)
                except Exception:
                    selected_total = 0
        self._pl_active = bool(playlist_mode)
        self._pl_selected_total = int(selected_total) if int(selected_total) > 0 else 0
        self._pl_ord = 0

        self._set_last_download_path(None)
        self._busy_push("download")
        self._ignore_progress_updates = False
        self._last_speed_mbps = None
        self._last_eta = None
        self.progress.set_show_text(True)
        self.progress.set_fraction(0.0)
        self.progress.set_text("%0")
        self.cancel_event = threading.Event()

        self.cancel_button.set_sensitive(True)
        self.download_button.set_sensitive(False)
        self.cancel_button.add_css_class("ytdl-cancel-hot")
        self.scan_button.set_sensitive(False)

        self._set_progress(0.0)
        self.set_status("download", "İndirme başlatılıyor...")

        def worker():
            try:
                out_path = download_video(
                    url,
                    self.output_dir,
                    format_key,
                    progress_cb=lambda p, sp=None, eta=None: GLib.idle_add(self._set_progress, p, sp, eta),
                    status_cb=lambda s: GLib.idle_add(self._status_from_core, s),
                    cancel_event=self.cancel_event,
                    format_override=getattr(self, "_format_overrides", {}).get(format_key),
                    playlist=playlist_mode,
                    playlist_items=(playlist_items_spec or None) if playlist_mode else None,
                )
                if out_path:
                    GLib.idle_add(self._set_last_download_path, out_path)
            except Exception as e:
                msg = str(e)
                if "iptal edildi" in msg.lower():
                    GLib.idle_add(self._clear_progress_text)
                    GLib.idle_add(self.set_status, "cancel", "İptal edildi", True)
                else:
                    # Ağ kopması / DNS vs. durumlarında "Worker hatası" gibi panikletici metni bastır.
                    if self._is_network_error_message(msg):
                        self._download_failed_due_to_net = True
                        self._net_was_down_during_download = True
                        # NetworkMonitor her zaman anında tetiklenmeyebiliyor; bu durumda da offline kabul ediyoruz.
                        self._net_available = False
                        GLib.idle_add(self._schedule_net_down_toast)
                        # Not: UI status alanına korkutucu uyarı basma; sadece toast ile yönet.
                        GLib.idle_add(self._suppress_worker_error_line)
                    else:
                        GLib.idle_add(self.set_status, "error", f"Worker hatası: {e}", True)
            finally:
                GLib.idle_add(self._reset_playlist_download_state)
                GLib.idle_add(self.cancel_button.set_sensitive, False)
                GLib.idle_add(self.cancel_button.remove_css_class, "ytdl-cancel-hot")
                GLib.idle_add(self.download_button.set_sensitive, True)
                GLib.idle_add(self.scan_button.set_sensitive, True)
                GLib.idle_add(self._busy_pop, "download")
                GLib.idle_add(self._clear_cancel_event)

        run_in_thread(worker)
    def _clear_cancel_event(self):
        self.cancel_event = None


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.sentor.youtubedownloader")


    def do_activate(self):
        win = MainWindow(self)
        win.present()
        GLib.timeout_add(80, try_center_window, win)


if __name__ == "__main__":
    App().run()