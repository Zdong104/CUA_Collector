"""
Platform-specific backends for screenshot, cursor tracking, and input monitoring.
Supports: Windows, macOS, Linux (X11), Linux (Wayland/GNOME)
"""
import os
import sys
import re
import time
import platform
import subprocess
import shutil
import threading
from typing import Tuple, Optional, Dict, Callable

# ============================================================
# Platform Detection
# ============================================================

def detect_platform() -> Tuple[str, str]:
    """Returns (os_name, session_type)."""
    system = platform.system().lower()
    if system == 'windows':
        return 'windows', 'windows'
    elif system == 'darwin':
        return 'macos', 'macos'
    elif system == 'linux':
        session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
        if session_type == 'wayland':
            desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
            if 'gnome' in desktop:
                return 'linux', 'wayland-gnome'
            elif 'kde' in desktop:
                return 'linux', 'wayland-kde'
            elif 'sway' in desktop:
                return 'linux', 'wayland-sway'
            return 'linux', 'wayland'
        return 'linux', 'x11'
    return system, 'unknown'


OS_NAME, SESSION_TYPE = detect_platform()


def get_screen_resolution() -> Tuple[int, int]:
    """Get primary screen resolution."""
    try:
        if SESSION_TYPE in ('windows', 'macos', 'x11'):
            import mss
            with mss.mss() as sct:
                m = sct.monitors[0]
                return (m['width'], m['height'])
        # Wayland: try xrandr or GNOME Shell
        r = subprocess.run(['xrandr', '--current'], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            match = re.search(r'current\s+(\d+)\s*x\s*(\d+)', r.stdout)
            if match:
                return (int(match.group(1)), int(match.group(2)))
    except Exception:
        pass
    return (1920, 1080)


# ============================================================
# Screenshot Backend
# ============================================================

class Screenshotter:
    """Cross-platform screenshot utility with automatic backend selection."""

    def __init__(self):
        self._wayland_backend = None
        self.method = self._detect_method()
        print(f"  📷 Screenshot backend: {self.method}")

    def _detect_method(self) -> str:
        if SESSION_TYPE in ('windows', 'macos', 'x11'):
            return 'mss'
        # Wayland: use PipeWire screencast (one-time approval, then free capture)
        if SESSION_TYPE.startswith('wayland'):
            return 'pipewire'
        # Fallback
        if shutil.which('grim'):
            return 'grim'
        return 'mss'

    def init_wayland(self):
        """Initialize Wayland screenshot backend. Call after startup banner."""
        if self.method == 'pipewire':
            from screenshot_wayland import create_wayland_screenshotter
            self._wayland_backend = create_wayland_screenshotter()
            self.method = 'pipewire' if self._wayland_backend else 'mss'

    def capture(self, output_path: str) -> bool:
        """Take a screenshot and save to output_path. Returns True on success."""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        try:
            if self.method == 'pipewire' and self._wayland_backend:
                return self._wayland_backend.capture(output_path)
            elif self.method == 'mss':
                return self._capture_mss(output_path)
            elif self.method == 'grim':
                return subprocess.run(['grim', output_path], capture_output=True, timeout=10).returncode == 0
            elif self.method == 'gnome-screenshot-cli':
                return subprocess.run(['gnome-screenshot', '-f', output_path], capture_output=True, timeout=10).returncode == 0
        except Exception as e:
            print(f"  ❌ Screenshot error ({self.method}): {e}")
        return False

    def stop(self):
        """Clean up screenshot resources."""
        if self._wayland_backend:
            self._wayland_backend.stop()

    def _capture_mss(self, output_path: str) -> bool:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.save(output_path, "PNG")
        return True


# ============================================================
# Cursor Position Backend
# ============================================================

class CursorTracker:
    """Cross-platform cursor position tracking."""

    def __init__(self):
        self.method = self._detect_method()
        print(f"  🖱️  Cursor tracking: {self.method}")

    def _detect_method(self) -> str:
        if SESSION_TYPE in ('windows', 'macos', 'x11'):
            return 'pynput'
        if 'gnome' in SESSION_TYPE and self._test_gnome_eval():
            return 'gnome-eval'
        return 'pynput'

    def _test_gnome_eval(self) -> bool:
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Shell',
                '--object-path', '/org/gnome/Shell',
                '--method', 'org.gnome.Shell.Eval',
                'global.get_pointer()'
            ], capture_output=True, text=True, timeout=3)
            return r.returncode == 0 and 'true' in r.stdout.lower()
        except Exception:
            return False

    def get_position(self) -> Tuple[int, int]:
        if self.method == 'gnome-eval':
            return self._get_gnome()
        return self._get_pynput()

    def _get_gnome(self) -> Tuple[int, int]:
        try:
            r = subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Shell',
                '--object-path', '/org/gnome/Shell',
                '--method', 'org.gnome.Shell.Eval',
                'global.get_pointer()'
            ], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                nums = re.findall(r'\d+', r.stdout)
                if len(nums) >= 2:
                    return (int(nums[0]), int(nums[1]))
        except Exception:
            pass
        return (0, 0)

    def _get_pynput(self) -> Tuple[int, int]:
        try:
            from pynput.mouse import Controller
            return Controller().position
        except Exception:
            return (0, 0)


# ============================================================
# Input Monitor - Wayland (evdev)
# ============================================================

class WaylandInputMonitor:
    """Monitor keyboard and mouse input via evdev on Wayland."""

    def __init__(self, callbacks: Dict[str, Callable]):
        self.callbacks = callbacks
        self._running = False
        self._threads = []
        self._ctrl_pressed = False
        self._devices = []

    def start(self):
        import evdev
        self._running = True
        all_devices = [evdev.InputDevice(path) for path in evdev.list_devices()]

        for dev in all_devices:
            caps = dev.capabilities(verbose=True)
            is_keyboard = False
            is_mouse = False

            for key, events in caps.items():
                key_name = key[0] if isinstance(key, tuple) else key
                event_strs = [str(e) for e in events]
                if key_name == 'EV_KEY':
                    if any('KEY_A' in s for s in event_strs):
                        is_keyboard = True
                    if any('BTN_LEFT' in s for s in event_strs):
                        is_mouse = True
                if key_name == 'EV_REL':
                    if any('REL_WHEEL' in s for s in event_strs):
                        is_mouse = True

            if is_keyboard or is_mouse:
                self._devices.append((dev, is_keyboard, is_mouse))
                kind = []
                if is_keyboard:
                    kind.append('kbd')
                if is_mouse:
                    kind.append('mouse')
                print(f"  📡 Monitoring: {dev.name} ({'+'.join(kind)})")

        for dev, is_kbd, is_mouse in self._devices:
            t = threading.Thread(target=self._monitor_device, args=(dev, is_kbd, is_mouse), daemon=True)
            t.start()
            self._threads.append(t)

    def _monitor_device(self, device, is_keyboard: bool, is_mouse: bool):
        import evdev
        from evdev import ecodes

        try:
            for event in device.read_loop():
                if not self._running:
                    break

                if event.type == ecodes.EV_KEY:
                    key_event = evdev.categorize(event)

                    # Track Ctrl
                    if key_event.scancode in (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL):
                        self._ctrl_pressed = key_event.keystate != 0

                    # Hotkeys on key-down
                    if key_event.keystate == 1 and self._ctrl_pressed:
                        if key_event.scancode == ecodes.KEY_F8:
                            threading.Thread(target=self.callbacks['on_hotkey_start_task'], daemon=True).start()
                        elif key_event.scancode == ecodes.KEY_F9:
                            threading.Thread(target=self.callbacks['on_hotkey_screenshot'], daemon=True).start()
                        elif key_event.scancode == ecodes.KEY_F12:
                            threading.Thread(target=self.callbacks['on_hotkey_end_task'], daemon=True).start()

                    # ESC (no Ctrl needed) to drop current action
                    if key_event.keystate == 1 and key_event.scancode == ecodes.KEY_ESC:
                        threading.Thread(target=self.callbacks['on_hotkey_drop_action'], daemon=True).start()

                    # Mouse button press
                    if is_mouse and key_event.keystate == 1:
                        btn_map = {
                            ecodes.BTN_LEFT: 'left',
                            ecodes.BTN_RIGHT: 'right',
                            ecodes.BTN_MIDDLE: 'middle',
                        }
                        if key_event.scancode in btn_map:
                            self.callbacks['on_mouse_click'](btn_map[key_event.scancode])

                elif event.type == ecodes.EV_REL and is_mouse:
                    if event.code in (ecodes.REL_WHEEL, getattr(ecodes, 'REL_WHEEL_HI_RES', 11)):
                        self.callbacks['on_mouse_scroll'](0, event.value)
                    elif event.code in (ecodes.REL_HWHEEL, getattr(ecodes, 'REL_HWHEEL_HI_RES', 12)):
                        self.callbacks['on_mouse_scroll'](event.value, 0)

        except Exception as e:
            if self._running:
                print(f"  ⚠️  Device error ({device.name}): {e}")

    def stop(self):
        self._running = False


# ============================================================
# Input Monitor - pynput (X11 / Windows / macOS)
# ============================================================

class PynputInputMonitor:
    """Monitor keyboard and mouse input via pynput."""

    def __init__(self, callbacks: Dict[str, Callable]):
        self.callbacks = callbacks
        self._ctrl_pressed = False
        self._keyboard_listener = None
        self._mouse_listener = None

    def start(self):
        from pynput import mouse, keyboard

        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
        self._mouse_listener = mouse.Listener(
            on_click=self._on_click,
            on_scroll=self._on_scroll
        )
        self._keyboard_listener.start()
        self._mouse_listener.start()
        print("  📡 Monitoring via pynput (keyboard + mouse)")

    def _on_key_press(self, key):
        from pynput import keyboard
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_pressed = True
        if self._ctrl_pressed:
            if key == keyboard.Key.f8:
                threading.Thread(target=self.callbacks['on_hotkey_start_task'], daemon=True).start()
            elif key == keyboard.Key.f9:
                threading.Thread(target=self.callbacks['on_hotkey_screenshot'], daemon=True).start()
            elif key == keyboard.Key.f12:
                threading.Thread(target=self.callbacks['on_hotkey_end_task'], daemon=True).start()
        # ESC (no Ctrl needed) to drop current action
        if key == keyboard.Key.esc:
            threading.Thread(target=self.callbacks['on_hotkey_drop_action'], daemon=True).start()

    def _on_key_release(self, key):
        from pynput import keyboard
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_pressed = False

    def _on_click(self, x, y, button, pressed):
        if pressed:
            self.callbacks['on_mouse_click'](button.name if hasattr(button, 'name') else str(button))

    def _on_scroll(self, x, y, dx, dy):
        self.callbacks['on_mouse_scroll'](dx, dy)

    def stop(self):
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()
