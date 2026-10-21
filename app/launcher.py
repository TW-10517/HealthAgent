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

# ── Wheel geometry ─────────────────────────────────────────────────────────
WHEEL_SIZE = 150   # fixed window size (square); transparent areas collapse visually
CENTER_R   = 32    # center button radius
INNER_R    = 38    # inner edge of segment ring (gap between button and segments)
OUTER_R    = 72    # outer edge of segment ring
SEG_GAP    = 5     # degrees of dark gap between each segment

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
    W  = WHEEL_SIZE * S       # canvas at 2× (300×300)
    img  = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = W // 2

    # Scaled geometry
    c_r   = CENTER_R * S
    in_r  = INNER_R  * S
    ou_r  = OUTER_R  * S

    t      = max(0.0, min(1.0, anim_t))
    outer  = in_r + int((ou_r - in_r) * t)
    seg_a  = int(255 * min(t * 1.8, 1.0))

    if t > 0.02:
        # ── Segments ──────────────────────────────────────────────────────
        for i, (sid, angle_c, accent, icon_fn, label) in enumerate(SEGMENTS):
            start = angle_c - 30 + SEG_GAP / 2
            end   = angle_c + 30 - SEG_GAP / 2
            is_hover = (i == hover_idx)
            if is_hover:
                # Brighter fill for hovered
                r, g, b = accent
                fill = (min(r + 40, 255), min(g + 40, 255), min(b + 40, 255), seg_a)
            else:
                fill = (18, 28, 46, seg_a)
            bbox = [cx - outer, cy - outer, cx + outer, cy + outer]
            draw.pieslice(bbox, start=start, end=end, fill=fill)

        # Thin inner border ring on segments
        border_r = outer - 2
        draw.ellipse([cx - border_r, cy - border_r, cx + border_r, cy + border_r],
                     outline=(45, 65, 90, seg_a), width=2)

        # Punch center hole
        hole_r = in_r + 2
        draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r],
                     fill=(0, 0, 0, 0))

        # ── Icons + labels ─────────────────────────────────────────────────
        icon_t = max(0.0, (t - 0.4) * 1.7)
        icon_a = int(255 * min(icon_t, 1.0))
        if icon_a > 0:
            fnt_label = _get_font(16)    # 16px at 2× → ~8px after downsample
            mid_r = (in_r + outer) / 2

            for i, (sid, angle_c, accent, icon_fn, label) in enumerate(SEGMENTS):
                is_hover = (i == hover_idx)
                ico_col = (255, 255, 255, icon_a) if is_hover else (*accent, icon_a)
                rad = math.radians(angle_c)
                ix  = cx + mid_r * math.cos(rad)
                iy  = cy + mid_r * math.sin(rad)
                _draw_icon(draw, icon_fn, int(ix), int(iy), ico_col, scale=S)
                if is_hover and icon_a > 80:
                    try:
                        bb = draw.textbbox((0, 0), label, font=fnt_label)
                        tw, th = bb[2] - bb[0], bb[3] - bb[1]
                    except Exception:
                        tw, th = len(label) * 10, 16
                    lx = max(4, min(int(ix) - tw // 2, W - tw - 4))
                    ly = max(4, min(int(iy) + 16, W - th - 4))
                    # Drop shadow for label
                    draw.text((lx + 1, ly + 1), label,
                              fill=(0, 0, 0, icon_a // 2), font=fnt_label)
                    draw.text((lx, ly), label,
                              fill=(255, 255, 255, icon_a), font=fnt_label)

    # ── Center button — clean solid button with subtle depth ─────────────
    # Drop shadow (soft offset ellipse, fully transparent at edges)
    draw.ellipse(
        [cx - c_r + 2, cy - c_r + 6, cx + c_r + 2, cy + c_r + 6],
        fill=(0, 0, 0, 50),
    )
    # Outer border ring (brand teal)
    draw.ellipse(
        [cx - c_r, cy - c_r, cx + c_r, cy + c_r],
        fill=(45, 212, 192, 255),
    )
    # Button face — dark navy tile, matching the app's "Orbit Pulse" logo
    draw.ellipse(
        [cx - c_r + 4, cy - c_r + 4, cx + c_r - 4, cy + c_r - 4],
        fill=(13, 20, 34, 255),
    )
    # Single subtle highlight arc at top-left (low opacity, narrow)
    draw.arc(
        [cx - c_r + 6, cy - c_r + 6, cx + c_r - 6, cy + c_r - 6],
        start=210, end=310, fill=(255, 255, 255, 40), width=4,
    )

    # Orbit Pulse mark (gauge arc + heartbeat pulse + end dot), scaled to button
    _draw_pulse_mark(draw, cx, cy, c_r)

    # Downsample 2× → final size for smooth anti-aliasing
    img = img.resize((WHEEL_SIZE, WHEEL_SIZE), Image.LANCZOS)

    # Convert RGBA → BGRA (Win32 DIB format)
    r, g, b, a = img.split()
    bgra = Image.merge("RGBA", (b, g, r, a))
    return bgra.tobytes()


def _draw_pulse_mark(draw, cx: int, cy: int, c_r: int):
    """Orbit Pulse mark — teal gauge arc + cyan heartbeat pulse + white end dot.
    Matches the SVG logo in desktop_app.py (viewBox 0 0 24 24)."""
    # Fit the 24x24 logo inside the button face (leave ~3px padding).
    r = max(6, c_r - 6)
    # Gauge arc: 220°, open at the top (matches SVG path M5.9 17.1 A 1 1 … 18.1 17.1).
    draw.arc(
        [cx - r, cy - r, cx + r, cy + r],
        start=140, end=400,
        fill=(45, 212, 192, 255),
        width=max(2, r // 4),
    )
    # Heartbeat pulse (SVG: M6 12 H9.3 L10.8 8.8 L12.8 15.2 L14.2 12 H16.7).
    k = r / 10.0  # SVG units -> px scale (radius ≈ 10 in SVG)
    pts = [
        (cx + (6   - 12) * k, cy + (12  - 12) * k),
        (cx + (9.3 - 12) * k, cy + (12  - 12) * k),
        (cx + (10.8- 12) * k, cy + (8.8 - 12) * k),
        (cx + (12.8- 12) * k, cy + (15.2- 12) * k),
        (cx + (14.2- 12) * k, cy + (12  - 12) * k),
        (cx + (16.7- 12) * k, cy + (12  - 12) * k),
    ]
    pw = max(2, int(r // 5))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i+1]], fill=(138, 240, 255, 255), width=pw)
    # End dot (SVG cx16.9 cy12 r1.35).
    dot_r = max(2, int(r * 0.14))
    dcx = int(cx + (16.9 - 12) * k)
    dcy = int(cy + (12   - 12) * k)
    draw.ellipse([dcx - dot_r, dcy - dot_r, dcx + dot_r, dcy + dot_r],
                 fill=(255, 255, 255, 255))


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
        for bx, bh in [(-5, 3), (-1, 6), (3, 4)]:
            draw.rectangle([cx + bx*s, iy + 5*s - bh*s, cx + bx*s + 2*s, iy + 5*s], fill=color)

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
            self.CLASS_NAME, "PCHealth Launcher",
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

        if self._expanded and self._anim_progress > 0.4:
            outer = INNER_R + (OUTER_R - INNER_R) * self._anim_progress
            if INNER_R < dist <= outer:
                angle = math.degrees(math.atan2(y - cy, x - cx))
                for i, (sid, angle_c, *_) in enumerate(SEGMENTS):
                    diff = ((angle - angle_c + 180) % 360) - 180
                    if abs(diff) <= 30 - SEG_GAP / 2:
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
        hwnd = user32.FindWindowW(None, "PC Health Agent")
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
