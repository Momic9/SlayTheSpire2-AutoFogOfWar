"""
fog_war.py
持续检测屏幕上是否出现 source/ 目录中的图片，
一旦检测到，就用鼠标右键在该图片所在位置涂抹 120×120 像素的区块。

按 F9 暂停/继续，按 ESC 退出。
"""

import os
import time
import threading
import ctypes

import cv2
import numpy as np
import pyautogui
from PIL import ImageGrab

# ── 配置 ──────────────────────────────────────────────────────────────────────
SOURCE_DIR      = os.path.join(os.path.dirname(__file__), "source")
SCAN_INTERVAL   = 0.3          # 每次截图间隔（秒）
MATCH_THRESHOLD = 0.80         # 模板匹配置信度阈值（0~1）
PAINT_SIZE      = 150          # 涂抹区块边长（像素）
PAINT_DURATION  = 1.5         # 每次涂抹持续时间（秒）
PAINT_STEP      = 3           # 涂抹步长（像素）
COOLDOWN        = 2.0          # 同一位置涂抹后的冷却时间（秒）
# ─────────────────────────────────────────────────────────────────────────────

pyautogui.FAILSAFE = True    # 鼠标移到左上角可紧急停止
pyautogui.PAUSE   = 0.0      # 禁用 pyautogui 每次操作后的默认 0.1 秒延迟

# 全局暂停标志（使用 threading.Event 保证线程间可见性）
_pause_event = threading.Event()   # set = 暂停，clear = 运行

def is_paused() -> bool:
    return _pause_event.is_set()

def toggle_pause():
    if _pause_event.is_set():
        _pause_event.clear()
        print("\n[F9] 继续")
    else:
        _pause_event.set()
        print("\n[F9] 暂停")

def load_templates(source_dir: str) -> dict:
    """加载 source 目录下所有 PNG 模板，返回 {文件名: BGR numpy数组}"""
    templates = {}
    for fname in os.listdir(source_dir):
        if fname.lower().endswith(".png"):
            path = os.path.join(source_dir, fname)
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is not None:
                templates[fname] = img
                print(f"[模板] 已加载: {fname}  尺寸: {img.shape[1]}×{img.shape[0]}")
    return templates


def grab_screen_bgr() -> np.ndarray:
    """截取全屏并转为 BGR numpy 数组"""
    pil_img = ImageGrab.grab()
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def find_all_matches(screen_bgr: np.ndarray,
                     template_bgr: np.ndarray,
                     threshold: float) -> list[tuple[int, int]]:
    """
    在屏幕截图中查找模板的所有匹配位置。
    返回匹配中心点列表 [(cx, cy), ...]（屏幕坐标）。
    """
    result = cv2.matchTemplate(screen_bgr, template_bgr, cv2.TM_CCOEFF_NORMED)
    locs = np.where(result >= threshold)
    h, w = template_bgr.shape[:2]

    centers = []
    for pt in zip(*locs[::-1]):   # (x, y)
        cx = pt[0] + w // 2
        cy = pt[1] + h // 2
        centers.append((cx, cy))

    # 非极大值抑制：合并距离过近的点
    merged = []
    for c in centers:
        if all(abs(c[0] - m[0]) > w // 2 or abs(c[1] - m[1]) > h // 2
               for m in merged):
            merged.append(c)
    return merged


# ── 底层鼠标操作（直接调用 Win32 API，比 pyautogui 快得多）────────────────────
_user32 = ctypes.windll.user32

def _move_mouse(x: int, y: int):
    """直接通过 Win32 SetCursorPos 移动鼠标（无延迟）"""
    _user32.SetCursorPos(int(x), int(y))

def _mouse_down_right():
    _user32.mouse_event(0x0008, 0, 0, 0, 0)   # MOUSEEVENTF_RIGHTDOWN

def _mouse_up_right():
    _user32.mouse_event(0x0010, 0, 0, 0, 0)   # MOUSEEVENTF_RIGHTUP


def right_click_paint(cx: int, cy: int, size: int = PAINT_SIZE,
                      duration: float = PAINT_DURATION,
                      step: int = PAINT_STEP) -> bool:
    """
    以 (cx, cy) 为中心，按住鼠标右键在 size×size 的区块内来回涂抹。
    采用 S 形扫描路径，使用 Win32 API 直接操作鼠标以获得最快速度。
    若涂抹过程中检测到暂停，立即抬起鼠标并返回 False；正常完成返回 True。
    """
    half = size // 2
    x0, y0 = cx - half, cy - half
    x1, y1 = cx + half, cy + half

    rows = list(range(y0, y1 + 1, step))
    if not rows:
        rows = [cy]

    # 构建完整的 S 形路径点列表
    path = []
    for i, y in enumerate(rows):
        if i % 2 == 0:
            path.append((x0, y))
            path.append((x1, y))
        else:
            path.append((x1, y))
            path.append((x0, y))

    # 计算总步数，用于均匀分配时间（忙等待，避免 sleep 精度问题）
    total_steps = 0
    for k in range(1, len(path)):
        dx = abs(path[k][0] - path[k-1][0])
        dy = abs(path[k][1] - path[k-1][1])
        total_steps += (dx + dy) // step

    step_interval = duration / total_steps if total_steps > 0 else 0

    # 移动到起点，按下右键
    _move_mouse(path[0][0], path[0][1])
    _mouse_down_right()

    cur_x, cur_y = path[0]
    deadline = time.perf_counter()  # 用于忙等待的时间基准

    for k in range(1, len(path)):
        if is_paused():
            _mouse_up_right()
            print(f"  → 涂抹被暂停中断: 中心({cx}, {cy})")
            return False

        tx, ty = path[k]

        # 水平移动（按 step 步长跳跃）
        if tx != cur_x:
            d = step if tx > cur_x else -step
            x = cur_x + d
            while (d > 0 and x <= tx) or (d < 0 and x >= tx):
                _move_mouse(x, cur_y)
                deadline += step_interval
                # 忙等待到 deadline，避免 sleep 的 15ms 精度问题
                while time.perf_counter() < deadline:
                    pass
                x += d
            cur_x = tx
            _move_mouse(cur_x, cur_y)

        # 垂直移动（按 step 步长跳跃）
        if ty != cur_y:
            d = step if ty > cur_y else -step
            y = cur_y + d
            while (d > 0 and y <= ty) or (d < 0 and y >= ty):
                _move_mouse(cur_x, y)
                deadline += step_interval
                while time.perf_counter() < deadline:
                    pass
                y += d
            cur_y = ty
            _move_mouse(cur_x, cur_y)

    _mouse_up_right()
    print(f"  → 涂抹完成: 中心({cx}, {cy})  区块 {size}×{size}px")
    return True


def keyboard_listener():
    """监听 F9（暂停/继续）和 ESC（退出）"""
    VK_F9  = 0x78
    VK_ESC = 0x1B

    # 使用 Windows API 轮询按键状态
    while True:
        if ctypes.windll.user32.GetAsyncKeyState(VK_ESC) & 0x8000:
            print("\n[ESC] 退出程序")
            os._exit(0)
        if ctypes.windll.user32.GetAsyncKeyState(VK_F9) & 0x8000:
            toggle_pause()
            time.sleep(0.5)   # 防抖
        time.sleep(0.05)


def main():
    print("=" * 50)
    print("  FogWar 自动涂抹工具")
    print("  F9  暂停 / 继续")
    print("  ESC 退出")
    print("  鼠标移到屏幕左上角紧急停止")
    print("=" * 50)

    templates = load_templates(SOURCE_DIR)
    if not templates:
        print("[错误] source 目录中没有找到任何 PNG 图片，退出。")
        return

    # 启动键盘监听线程
    t = threading.Thread(target=keyboard_listener, daemon=True)
    t.start()

    # 记录每个位置的最后涂抹时间，避免重复涂抹
    last_painted: dict[tuple[int, int], float] = {}

    print("\n[开始] 正在扫描屏幕...\n")

    while True:
        if is_paused():
            time.sleep(0.2)
            continue

        screen = grab_screen_bgr()
        now = time.time()

        for fname, tmpl in templates.items():
            if is_paused():
                continue
            matches = find_all_matches(screen, tmpl, MATCH_THRESHOLD)
            for (cx, cy) in matches:
                
                if is_paused():
                    continue
                # 冷却检查：找最近的已涂抹点
                key = (cx // 20 * 20, cy // 20 * 20)   # 量化到 20px 格
                if now - last_painted.get(key, 0) < COOLDOWN:
                    continue

                print(f"[检测] {fname}  位置: ({cx}, {cy})")
                right_click_paint(cx, cy)
                last_painted[key] = time.time()

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
