"""Helix — Floating desktop launcher with segment-wheel radial menu."""
import ctypes
import ctypes.wintypes as wt
import json
import math
import sys
import threading
from pathlib import Path

import db

_POSITION_KEY = "launcher_pos"
_ENABLED_KEY  = "launcher_enabled"
_STARTUP_KEY  = "launcher_startup"

_launcher = None
_lock = threading.Lock()

# ── Win32 constants ────────────────────────────────────────────────────────
WS_EX_TOPMOST    = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED    = 0x00080000
WS_POPUP         = 0x80000000
WM_DESTROY       = 0x0002
WM_PAINT         = 0x000F
WM_ERASEBKGND    = 0x0014
WM_TIMER         = 0x0113
WM_LBUTTONDOWN   = 0x0201
WM_LBUTTONUP     = 0x0202
WM_MOUSEMOVE     = 0x0200
WM_RBUTTONUP     = 0x0205
WM_MOUSELEAVE    = 0x02A3
CS_DBLCLKS       = 0x0008
MK_LBUTTON       = 0x0001
ULW_ALPHA        = 0x00000002
SW_RESTORE       = 9
IDC_ARROW        = 32512
IDC_HAND         = 32649
TME_LEAVE        = 0x00000002
SWP_NOSIZE       = 0x0001
SWP_NOZORDER     = 0x0004

user32   = ctypes.windll.user32
gdi32    = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
)
user32.DefWindowProcW.restype  = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style",         ctypes.c_uint),
        ("lpfnWndProc",   WNDPROC),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     wt.HINSTANCE),
        ("hIcon",         wt.HICON),
        ("hCursor",       wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName",  wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


wt.WNDCLASSW = WNDCLASSW

# ── Launcher geometry (dock-tile layout) ───────────────────────────────────
WHEEL_SIZE = 200   # fixed window size (square); transparent areas collapse visually
CENTER_R   = 30    # center button radius
TILE_R     = 64    # distance from center to each tile center (ring radius)
TILE_SIZE  = 20    # tile radius (idle); minimal flat tiles
TILE_HOVER = 23    # tile radius (hover) — subtle pop, not drastic
# Legacy aliases kept so other code (hit-test fallbacks, UI pills) keeps working.
INNER_R    = TILE_R - TILE_SIZE
OUTER_R    = TILE_R + TILE_SIZE
SEG_GAP    = 0

ANIM_STEPS     = 10
TIMER_ANIM     = 1
TIMER_COLLAPSE = 2
COLLAPSE_DELAY = 3500   # ms before auto-collapse after mouse leave

# Segments: (id, center_angle_deg, accent_rgb, icon_fn, label)
# Angles follow standard math convention (0=right, +CCW in image coords = CW visually)
# 6 segments × 60° each; half-span = 30° (see _render_frame / hit-test)
SEGMENTS = [
    ("open", -90,  (0,  212, 170),  "icon_open",  "Open"),
    ("chat", -30,  (60, 100, 220),  "icon_chat",  "Chat"),
    ("scan",  30,  (251, 160,  36), "icon_scan",  "Scan"),
    ("mon",   90,  (0,  220, 100),  "icon_mon",   "HUD"),
    ("hide", 150,  (80,   95, 115), "icon_hide",  "Hide"),
    ("exit", 210,  (220,  50,  50), "icon_exit",  "Exit"),
]

# Pre-load font once
_FONT_CACHE: dict = {}


def _get_font(size: int = 7):
    if size not in _FONT_CACHE:
        from PIL import ImageFont
        for path in [
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ]:
            try:
                _FONT_CACHE[size] = ImageFont.truetype(path, size)
                break
            except Exception:
                pass
        if size not in _FONT_CACHE:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


# ── Pillow rendering ───────────────────────────────────────────────────────

def _render_frame(anim_t: float, hover_idx: int) -> bytes:
    """Render the segment wheel to raw BGRA bytes (WHEEL_SIZE × WHEEL_SIZE).
    Renders at 2× resolution and downsamples with LANCZOS for smooth anti-aliasing."""
    from PIL import Image, ImageDraw, ImageFilter

    S  = 2                    # supersampling scale
    W  = WHEEL_SIZE * S       # canvas at 2× (400×400)
    img  = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = W // 2

    # Scaled geometry
    c_r   = CENTER_R * S
    ring_r = TILE_R * S
    tile_r_idle  = TILE_SIZE  * S
    tile_r_hover = TILE_HOVER * S

    t      = max(0.0, min(1.0, anim_t))
    # Keep old vars for any legacy code paths below.
    in_r  = INNER_R * S
    ou_r  = OUTER_R * S
    outer = in_r + int((ou_r - in_r) * t)
    seg_a = int(255 * min(t * 1.8, 1.0))

    if t > 0.02:
        # ── Minimal flat tiles ────────────────────────────────────────────
        # One small flat accent-colored circle per action. No hover label —
        # icons alone identify the action, and the hover state is a bright
        # ring + slight scale-up. No floating text that could cover a tile.
        for i, (sid, angle_c, accent, icon_fn, label) in enumerate(SEGMENTS):
            r, g, b = accent
            is_hover = (i == hover_idx)

            rad = math.radians(angle_c)
            ring_t = ring_r * t
            tx = cx + ring_t * math.cos(rad)
            ty = cy + ring_t * math.sin(rad)
            tr = tile_r_hover if is_hover else tile_r_idle

            if is_hover:
                fr, fg, fb = (min(r + 30, 255), min(g + 30, 255), min(b + 30, 255))
            else:
                fr, fg, fb = r, g, b
            body_a = int(seg_a * (0.98 if is_hover else 0.88))
            draw.ellipse([tx - tr, ty - tr, tx + tr, ty + tr],
                         fill=(fr, fg, fb, body_a))

            # Hover: bright white ring hugging the tile edge — clear, strong
            # feedback that doesn't rely on text.
            if is_hover:
                draw.ellipse([tx - tr, ty - tr, tx + tr, ty + tr],
                             outline=(255, 255, 255, int(seg_a * 0.95)),
                             width=max(2, S))
            else:
                draw.ellipse([tx - tr, ty - tr, tx + tr, ty + tr],
                             outline=(255, 255, 255, int(seg_a * 0.28)),
                             width=max(1, S // 2))

            # Icon — white, slightly larger when hovered.
            icon_t = max(0.0, (t - 0.3) * 1.5)
            icon_a = int(255 * min(icon_t, 1.0))
            if icon_a > 0:
                ico_scale = S + (1 if is_hover else 0)
                _draw_icon(draw, icon_fn, int(tx), int(ty),
                           (255, 255, 255, icon_a), scale=ico_scale)

    # ── Center button — Helix "Orbit Pulse" mark (matches app_icon.ico) ───
    # Dark navy tile with teal rim, orbit-pulse mark painted on top.
    draw.ellipse(
        [cx - c_r + 2, cy - c_r + 5, cx + c_r + 2, cy + c_r + 5],
        fill=(0, 0, 0, 55),
    )
    draw.ellipse(
        [cx - c_r, cy - c_r, cx + c_r, cy + c_r],
        fill=(14, 20, 32, 255),  # dark navy base (matches app_icon.ico tile)
    )
    draw.ellipse(
        [cx - c_r + 1, cy - c_r + 1, cx + c_r - 1, cy + c_r - 1],
        outline=(45, 212, 192, 180), width=max(1, S // 2),
    )
    _draw_pulse_mark(draw, cx, cy, c_r)

    # Downsample 2× → final size for smooth anti-aliasing
    img = img.resize((WHEEL_SIZE, WHEEL_SIZE), Image.LANCZOS)

    # Convert RGBA → BGRA (Win32 DIB format)
    r, g, b, a = img.split()
    bgra = Image.merge("RGBA", (b, g, r, a))
    return bgra.tobytes()


def _draw_pulse_mark(draw, cx: int, cy: int, c_r: int):
    """Helix "Orbit Pulse" mark — teal gauge arc + cyan heartbeat line + white
    end dot. This is the actual Helix brand mark (matches app_icon.ico and the
    `_create_shield_icon` tile)."""
    r = max(6, c_r - 5)
    # Teal gauge arc — ~260° open at the top.
    draw.arc(
        [cx - r, cy - r, cx + r, cy + r],
        start=140, end=400,
        fill=(45, 220, 200, 255),
        width=max(2, r // 4),
    )
    # Heartbeat pulse — flat line with a dip then a peak.
    pts = [
        (cx - r * 0.60, cy),
        (cx - r * 0.26, cy),
        (cx - r * 0.08, cy - r * 0.42),
        (cx + r * 0.12, cy + r * 0.48),
        (cx + r * 0.30, cy),
        (cx + r * 0.60, cy),
    ]
    pw = max(2, int(r // 5))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=(130, 240, 255, 255), width=pw)
    # White end dot — sits at the right end of the pulse line.
    dot_r = max(2, int(r * 0.14))
    draw.ellipse(
        [cx + r * 0.60 - dot_r, cy - dot_r,
         cx + r * 0.60 + dot_r, cy + dot_r],
        fill=(255, 255, 255, 255),
    )


def _draw_icon(draw, icon_fn: str, cx: int, cy: int, color: tuple, scale: int = 1):
    """Draw a small monochrome icon in the segment."""
    s  = scale
    iy = cy

    if icon_fn == "icon_open":
        pts = [(cx - 5*s, iy - 5*s), (cx - 5*s, iy + 5*s), (cx + 6*s, iy)]
        draw.polygon(pts, fill=color)

    elif icon_fn == "icon_chat":
        draw.rounded_rectangle(
            [cx - 6*s, iy - 5*s, cx + 6*s, iy + 2*s],
            radius=2*s, outline=color, width=2*s,
        )
        draw.polygon([(cx - 4*s, iy + 2*s), (cx - 4*s, iy + 6*s), (cx, iy + 2*s)], fill=color)
        for dx in (-3, 0, 3):
            draw.ellipse([cx + dx*s - s, iy - 2*s, cx + dx*s + 2*s, iy + s], fill=color)

    elif icon_fn == "icon_scan":
        draw.ellipse([cx - 6*s, iy - 6*s, cx + s, iy + s], outline=color, width=2*s)
        draw.line([(cx, iy + s), (cx + 5*s, iy + 6*s)], fill=color, width=2*s)

    elif icon_fn == "icon_hide":
        draw.line([(cx - 5*s, iy - 2*s), (cx,       iy + 3*s)], fill=color, width=2*s)
        draw.line([(cx,       iy + 3*s), (cx + 5*s, iy - 2*s)], fill=color, width=2*s)

    elif icon_fn == "icon_mon":
        # Minimal bar chart — three vertical bars rising left→right. Matches
        # the Performance / Insights iconography used inside the main app.
        base_y = iy + 5*s
        for bx, bh in [(-5, 4), (-1, 7), (3, 5)]:
            draw.rounded_rectangle(
                [cx + bx*s, base_y - bh*s, cx + bx*s + 2*s, base_y],
                radius=max(1, s // 2),
                fill=color,
            )

    elif icon_fn == "icon_exit":
        draw.line([(cx - 5*s, iy - 5*s), (cx + 5*s, iy + 5*s)], fill=color, width=2*s)
        draw.line([(cx + 5*s, iy - 5*s), (cx - 5*s, iy + 5*s)], fill=color, width=2*s)


# ── GDI helpers ───────────────────────────────────────────────────────────

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          ctypes.c_uint32),
        ("biWidth",         ctypes.c_int32),
        ("biHeight",        ctypes.c_int32),
        ("biPlanes",        ctypes.c_uint16),
        ("biBitCount",      ctypes.c_uint16),
        ("biCompression",   ctypes.c_uint32),
        ("biSizeImage",     ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed",       ctypes.c_uint32),
        ("biClrImportant",  ctypes.c_uint32),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


def _make_layered_dc(bgra_bytes: bytes, w: int, h: int):
    """Create a memory DC with a 32-bit DIB for UpdateLayeredWindow.
    Returns (mem_dc, hbmp, old_hbmp, screen_dc) — caller must clean up."""
    screen_dc = user32.GetDC(None)
    mem_dc    = gdi32.CreateCompatibleDC(screen_dc)

    bih = _BITMAPINFOHEADER()
    bih.biSize     = ctypes.sizeof(_BITMAPINFOHEADER)
    bih.biWidth    = w
    bih.biHeight   = -h   # top-down
    bih.biPlanes   = 1
    bih.biBitCount = 32
    # biCompression = 0 (BI_RGB)

    ppv = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(
        screen_dc, ctypes.byref(bih), 0, ctypes.byref(ppv), None, 0,
    )
    ctypes.memmove(ppv, bgra_bytes, len(bgra_bytes))
    old_hbmp = gdi32.SelectObject(mem_dc, hbmp)
    return mem_dc, hbmp, old_hbmp, screen_dc


# ── Saved position helpers ─────────────────────────────────────────────────

def _get_saved_position():
    raw = db.get_setting(_POSITION_KEY)
    if raw:
        try:
            pos = json.loads(raw)
            return pos.get("x", None), pos.get("y", None)
        except Exception:
            pass
    return None, None


def _save_position(x, y):
    db.set_setting(_POSITION_KEY, json.dumps({"x": x, "y": y}))


def is_enabled():
    return db.get_setting(_ENABLED_KEY) != "0"


def set_enabled(enabled: bool):
    db.set_setting(_ENABLED_KEY, "1" if enabled else "0")


def _get_startup_enabled():
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, "PCHealthAgent")
            return True
    except (FileNotFoundError, OSError):
        return False


def _set_startup(enabled: bool):
    db.set_setting(_STARTUP_KEY, "1" if enabled else "0")
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "PCHealthAgent"
        if enabled:
            if getattr(sys, "frozen", False):
                # Running as a packaged .exe — launch the exe directly
                cmd = f'"{sys.executable}"'
            else:
                # Use pythonw.exe so no console window appears at login
                exe_path = sys.executable.replace("python.exe", "pythonw.exe")
                if not Path(exe_path).exists():
                    exe_path = sys.executable
                script = str(Path(__file__).parent / "desktop_app.py")
                cmd    = f'"{exe_path}" "{script}"'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
    except Exception:
        pass


# ── FloatingLauncher ───────────────────────────────────────────────────────

class FloatingLauncher:
    CLASS_NAME = "PCHealthSegmentWheel"

    def __init__(self, on_open_cb, on_scan_cb, on_quit_cb, on_chat_cb=None, on_monitor_cb=None):
        self._on_open    = on_open_cb
        self._on_scan    = on_scan_cb
        self._on_quit    = on_quit_cb
        self._on_chat    = on_chat_cb
        self._on_monitor = on_monitor_cb
        self._hwnd     = None
        self._thread   = None
        self._alive    = False
        self._expanded      = False
        self._anim_progress = 0.0
        self._dragging  = False
        self._drag_x    = 0
        self._drag_y    = 0
        self._moved     = False
        self._hover_seg = -1
        self._wndproc_ref = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self):
        if self._alive:
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._alive = False
        if self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass

    def hide(self):
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 0)

    def show(self):
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 5)

    # ── Win32 window loop ──────────────────────────────────────────────────

    def _run(self):
        hinstance = kernel32.GetModuleHandleW(None)

        wndclass = wt.WNDCLASSW()
        wndclass.style = CS_DBLCLKS
        self._wndproc_ref = WNDPROC(self._window_proc)
        wndclass.lpfnWndProc  = self._wndproc_ref
        wndclass.hInstance    = hinstance
        wndclass.hCursor      = user32.LoadCursorW(None, IDC_ARROW)
        wndclass.hbrBackground = 0
        wndclass.lpszClassName = self.CLASS_NAME

        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            self._alive = False
            return

        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)

        sx, sy = _get_saved_position()
        if sx is None:
            # Default: bottom-right corner with some margin
            sx = sw - WHEEL_SIZE - 24
            sy = sh - WHEEL_SIZE - 80
        sx = max(0, min(sx, sw - WHEEL_SIZE))
        sy = max(0, min(sy, sh - WHEEL_SIZE))

        self._hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED,
            self.CLASS_NAME, "Helix Launcher",
            WS_POPUP,
            sx, sy, WHEEL_SIZE, WHEEL_SIZE,
            None, None, hinstance, None,
        )
        if not self._hwnd:
            self._alive = False
            return

        self._redraw()
        user32.ShowWindow(self._hwnd, 5)
        user32.UpdateWindow(self._hwnd)

        msg = wt.MSG()
        while self._alive and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hwnd:
            user32.KillTimer(self._hwnd, TIMER_ANIM)
            user32.KillTimer(self._hwnd, TIMER_COLLAPSE)
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        user32.UnregisterClassW(self.CLASS_NAME, hinstance)
        self._alive = False

    # ── Rendering ──────────────────────────────────────────────────────────

    def _redraw(self):
        if not self._hwnd:
            return
        try:
            bgra = _render_frame(self._anim_progress, self._hover_seg)
            W = WHEEL_SIZE
            mem_dc, hbmp, old_hbmp, screen_dc = _make_layered_dc(bgra, W, W)

            rect = wt.RECT()
            user32.GetWindowRect(self._hwnd, ctypes.byref(rect))
            pt_dst = _POINT(rect.left, rect.top)
            pt_src = _POINT(0, 0)
            sz     = _SIZE(W, W)
            # BLENDFUNCTION: BlendOp=AC_SRC_OVER(0), Flags=0, Alpha=255, Format=AC_SRC_ALPHA(1)
            blend  = (ctypes.c_byte * 4)(0, 0, -1, 1)  # 255 = 0xFF = -1 as signed byte

            user32.UpdateLayeredWindow(
                self._hwnd, None,
                ctypes.byref(pt_dst), ctypes.byref(sz),
                mem_dc, ctypes.byref(pt_src),
                0, ctypes.byref(blend), ULW_ALPHA,
            )

            gdi32.SelectObject(mem_dc, old_hbmp)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)
        except Exception:
            pass

    # ── Hit testing ────────────────────────────────────────────────────────

    def _hit_test(self, x, y):
        cx = cy = WHEEL_SIZE // 2
        dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)

        if dist <= CENTER_R:
            return "center"

        # Tile hit test: each action is a round tile at TILE_R from center;
        # tiles are animated outward from the center as _anim_progress grows.
        if self._expanded and self._anim_progress > 0.3:
            ring = TILE_R * self._anim_progress
            # Generous hit radius so the pointer snaps to the nearest tile even
            # if it's slightly off — matches the visible hover feedback.
            hit_r = TILE_HOVER + 4
            for i, (sid, angle_c, *_) in enumerate(SEGMENTS):
                rad = math.radians(angle_c)
                tx = cx + ring * math.cos(rad)
                ty = cy + ring * math.sin(rad)
                if (x - tx) ** 2 + (y - ty) ** 2 <= hit_r ** 2:
                    return i
        return None

    # ── Animation & expand/collapse ────────────────────────────────────────

    def _toggle_expand(self):
        self._expanded = not self._expanded
        if self._expanded:
            user32.KillTimer(self._hwnd, TIMER_COLLAPSE)
        user32.SetTimer(self._hwnd, TIMER_ANIM, 16, None)

    def _collapse(self):
        if self._expanded:
            self._expanded = False
            user32.SetTimer(self._hwnd, TIMER_ANIM, 16, None)

    # ── Message handler ────────────────────────────────────────────────────

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_ERASEBKGND:
            return 1

        elif msg == WM_PAINT:
            ps = (ctypes.c_byte * 72)()
            user32.BeginPaint(hwnd, ctypes.byref(ps))
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0

        elif msg == WM_TIMER:
            if wparam == TIMER_ANIM:
                target = 1.0 if self._expanded else 0.0
                step   = 1.0 / ANIM_STEPS
                if self._anim_progress < target:
                    self._anim_progress = min(self._anim_progress + step, 1.0)
                elif self._anim_progress > target:
                    self._anim_progress = max(self._anim_progress - step, 0.0)
                if abs(self._anim_progress - target) < 0.01:
                    self._anim_progress = target
                    user32.KillTimer(hwnd, TIMER_ANIM)
                self._redraw()
                return 0
            elif wparam == TIMER_COLLAPSE:
                user32.KillTimer(hwnd, TIMER_COLLAPSE)
                self._collapse()
                return 0

        elif msg == WM_LBUTTONDOWN:
            x = lparam & 0xFFFF
            y = (lparam >> 16) & 0xFFFF
            if x > 32767: x -= 65536
            if y > 32767: y -= 65536
            hit = self._hit_test(x, y)
            if hit == "center":
                user32.SetCapture(hwnd)
                self._dragging = True
                self._moved    = False
                self._drag_x   = x
                self._drag_y   = y
            elif isinstance(hit, int):
                self._execute_segment(hit)
            else:
                self._collapse()
            return 0

        elif msg == WM_MOUSEMOVE:
            x = lparam & 0xFFFF
            y = (lparam >> 16) & 0xFFFF
            if x > 32767: x -= 65536
            if y > 32767: y -= 65536

            if self._dragging and (wparam & MK_LBUTTON):
                dx = x - self._drag_x
                dy = y - self._drag_y
                if abs(dx) > 2 or abs(dy) > 2:
                    self._moved = True
                    rect = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    new_x = rect.left + dx
                    new_y = rect.top  + dy
                    # Move via UpdateLayeredWindow's pptDst for a layered window
                    pt_dst = _POINT(new_x, new_y)
                    bgra   = _render_frame(self._anim_progress, self._hover_seg)
                    mem_dc, hbmp, old_hbmp, screen_dc = _make_layered_dc(
                        bgra, WHEEL_SIZE, WHEEL_SIZE
                    )
                    pt_src = _POINT(0, 0)
                    sz     = _SIZE(WHEEL_SIZE, WHEEL_SIZE)
                    blend  = (ctypes.c_byte * 4)(0, 0, -1, 1)
                    user32.UpdateLayeredWindow(
                        hwnd, None,
                        ctypes.byref(pt_dst), ctypes.byref(sz),
                        mem_dc, ctypes.byref(pt_src),
                        0, ctypes.byref(blend), ULW_ALPHA,
                    )
                    gdi32.SelectObject(mem_dc, old_hbmp)
                    gdi32.DeleteObject(hbmp)
                    gdi32.DeleteDC(mem_dc)
                    user32.ReleaseDC(None, screen_dc)
            else:
                hit = self._hit_test(x, y)
                new_hover = hit if isinstance(hit, int) else -1
                if new_hover != self._hover_seg:
                    self._hover_seg = new_hover
                    cursor = IDC_HAND if (new_hover >= 0 or hit == "center") else IDC_ARROW
                    user32.SetCursor(user32.LoadCursorW(None, cursor))
                    self._redraw()
                if self._expanded:
                    user32.KillTimer(hwnd, TIMER_COLLAPSE)
                    user32.SetTimer(hwnd, TIMER_COLLAPSE, COLLAPSE_DELAY, None)

                tme = (ctypes.c_uint * 4)()
                tme[0] = 16   # cbSize of TRACKMOUSEEVENT
                tme[1] = TME_LEAVE
                tme[2] = hwnd
                tme[3] = 0
                user32.TrackMouseEvent(ctypes.byref(tme))
            return 0

        elif msg == WM_LBUTTONUP:
            user32.ReleaseCapture()
            was_dragging   = self._dragging
            self._dragging = False
            if was_dragging:
                if self._moved:
                    rect = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    _save_position(rect.left, rect.top)
                else:
                    self._toggle_expand()
            return 0

        elif msg == WM_RBUTTONUP:
            self._toggle_expand()
            return 0

        elif msg == WM_MOUSELEAVE:
            if self._hover_seg >= 0:
                self._hover_seg = -1
                self._redraw()
            if self._expanded:
                user32.SetTimer(hwnd, TIMER_COLLAPSE, COLLAPSE_DELAY, None)
            return 0

        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _execute_segment(self, idx):
        self._collapse()
        sid = SEGMENTS[idx][0]
        if sid == "open":
            if self._on_open: self._on_open()
        elif sid == "chat":
            if self._on_chat: self._on_chat()
        elif sid == "scan":
            if self._on_scan: self._on_scan()
        elif sid == "mon":
            if self._on_monitor: self._on_monitor()
        elif sid == "hide":
            self.hide()
        elif sid == "exit":
            self.stop()
            if self._on_quit: self._on_quit()


# ── Single-instance mutex ──────────────────────────────────────────────────

_mutex_handle = None


def acquire_single_instance():
    global _mutex_handle
    try:
        _mutex_handle = kernel32.CreateMutexW(None, True, "PCHealthAgent_SingleInstance")
        if ctypes.GetLastError() == 183:
            kernel32.CloseHandle(_mutex_handle)
            _mutex_handle = None
            return False
        return True
    except Exception:
        return True


def bring_existing_to_front():
    try:
        try:
            from metadata import APP_NAME as _AN
        except Exception:
            _AN = "Helix"
        hwnd = user32.FindWindowW(None, _AN)
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def get_launcher():
    return _launcher


def start_launcher(on_open_cb, on_scan_cb, on_quit_cb, on_chat_cb=None, on_monitor_cb=None):
    global _launcher
    if not is_enabled():
        return None
    with _lock:
        if _launcher and _launcher._alive:
            return _launcher
        _launcher = FloatingLauncher(on_open_cb, on_scan_cb, on_quit_cb,
                                     on_chat_cb=on_chat_cb, on_monitor_cb=on_monitor_cb)
        _launcher.start()
        return _launcher
