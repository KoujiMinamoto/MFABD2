"""Fishing custom action for MaaFramework.

This ports the standalone Python+ADB fishing bot into MFABD2 as a Maa custom
action. It relies on Maa controller APIs for screencap and input, not raw ADB.

Defaults assume the game runs at 1920x1080. The original coordinates were
measured at 1280x720; all points are scaled at runtime based on the current
screenshot resolution. Override timing/strategy via pipeline argv.raw_json if
needed.

Migration Notes:
- cv2 dependency removed; uses MaaFramework pipeline ColorMatch directly for all color detection
- Progress bar analysis uses ColorMatch for cursor and zone tracking
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import numpy as np
from typing import Any, List, Optional, Tuple

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


# ==================== 基础配置 ====================

# 游标移动速度（像素/帧）
CURSOR_SPEED_PX_PER_FRAME = 4.2
# 蓝色区域每帧收缩像素数
BLUE_ZONE_SHRINK_PX_PER_FRAME = 0.83  

@dataclass
class TimingCfg:
    wait_fish_interval: float = 0.08
    after_cast: float = 0.2
    after_catch: float = 3.0
    input_delay: float = 0.055  # controller is fast; keep small buffer


@dataclass
class CoordCfg:
    cast_rod: Tuple[int, int] = (1130, 570)
    screen_center: Tuple[int, int] = (640, 360)
    progress_bar_left: int = 484
    progress_bar_right: int = 858
    minigame_area: Tuple[int, int, int, int] = (335,505,600,154)


class FishingBot:
    def __init__(
        self,
        context: Context,
        sell_interval: int = 30,
        timing: TimingCfg | None = None,
        coords: CoordCfg | None = None,
    ):
        self.context = context
        self.controller = context.tasker.controller
        self.sell_interval = sell_interval
        self.timing = timing or TimingCfg()
        self.coords = coords or CoordCfg()

        # runtime stats
        self.running = False
        self.fish_count = 0
        self.success_count = 0
        self.fish_since_last_sell = 0
        self.total_sell_count = 0

    # ============ Controller wrappers ============
    def tap(self, x: float, y: float):
        job = self.controller.post_click(int(x), int(y))
        start_time = time.time()
        job.wait()
        elapsed = time.time() - start_time
        print(f"    🖱️ 点击 ({int(x)}, {int(y)}) 耗时 {elapsed:.3f}s")

    def long_press(self, x: float, y: float, duration_ms: int = 1000):
        # emulate long press via swipe with zero distance
        job = self.controller.post_swipe(int(x), int(y), int(x), int(y), duration_ms)
        job.wait()

    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float, duration_ms: int = 500):
        job = self.controller.post_swipe(int(start_x), int(start_y), int(end_x), int(end_y), duration_ms)
        job.wait()

    def get_screenshot(self) -> Optional[Any]:
        job = self.controller.post_screencap()
        return job.wait().get()

    def delay(self, seconds: float):
        time.sleep(seconds)

    # ============ Detection methods ============
    def detect_exclamation(self, screenshot: Any) -> bool:
        """Detect fish hook indicator using pipeline TemplateMatch.
        
        Uses Detect_Took_Bait template matching for more accurate detection.
        """
        # Run pipeline recognition (MAA handles resolution scaling automatically)
        reco_result = self.context.run_recognition("Detect_Took_Bait", screenshot)
        print("Detect_Took_Bait result:", reco_result)
        return reco_result.hit

    # 进度条 ROI,与 pipeline Detect_Progress_* 节点一致 (x, y, w, h)
    BAR_ROI = (480, 602, 383, 20)

    @staticmethod
    def _bgr_to_hsv(bgr):
        """向量化 BGR→HSV(OpenCV 约定:H 0-180, S/V 0-255)。"""
        b = bgr[..., 0].astype(np.float32)
        g = bgr[..., 1].astype(np.float32)
        r = bgr[..., 2].astype(np.float32)
        v = np.maximum(np.maximum(b, g), r)
        mn = np.minimum(np.minimum(b, g), r)
        diff = v - mn
        s = np.where(v > 0, diff * 255.0 / np.maximum(v, 1.0), 0.0)
        h = np.zeros_like(v)
        m = diff > 0
        safe = np.maximum(diff, 1.0)
        rm = m & (v == r)
        gm = m & (v == g) & ~rm
        bm = m & (v == b) & ~rm & ~gm
        h[rm] = 60.0 * (g[rm] - b[rm]) / safe[rm]
        h[gm] = 120.0 + 60.0 * (b[gm] - r[gm]) / safe[gm]
        h[bm] = 240.0 + 60.0 * (r[bm] - g[bm]) / safe[bm]
        h = np.where(h < 0, h + 360.0, h) / 2.0
        return h, s, v

    @staticmethod
    def _mask_runs(mask, min_pixels, x0):
        """把列投影切成连续区段,返回 [(abs_start, abs_end), ...],过滤总像素不足的段。"""
        active = mask.any(axis=0)
        runs = []
        start = None
        for i, a in enumerate(active):
            if a and start is None:
                start = i
            elif not a and start is not None:
                if int(mask[:, start:i].sum()) >= min_pixels:
                    runs.append((start + x0, i + x0))
                start = None
        if start is not None and int(mask[:, start:].sum()) >= min_pixels:
            runs.append((start + x0, mask.shape[1] + x0))
        return runs

    def analyze_progress_bar(self, screenshot: Any):
        """本地 numpy 分析进度条(白游标/蓝区/黄区)。

        原实现每帧走 3 次 run_recognition RPC,在 PlayCover 等截图链路较慢
        的环境下单轮延迟可达 0.7s+,而游标 ~250px/s,预测必然过期。本地
        计算沿用 pipeline Detect_Progress_* 节点相同的 HSV 阈值,耗时 <10ms。
        """
        result = {"cursor_x": None, "blue_regions": [], "yellow_regions": [], "valid": False}
        x0, y0, w, h = self.BAR_ROI
        img = np.asarray(screenshot)
        if img.ndim != 3 or img.shape[0] < y0 + h or img.shape[1] < x0 + w:
            print("Progress bar analysis: 截图尺寸异常", getattr(img, "shape", None))
            return result
        bar = img[y0:y0 + h, x0:x0 + w, :3]
        H, S, V = self._bgr_to_hsv(bar)

        # 阈值与 pipeline 节点一致:白游标 S<=30,V>=240;蓝 H95-115;黄 H15-35
        white = (S <= 30) & (V >= 240)
        blue = (H >= 95) & (H <= 115) & (S >= 130) & (V >= 250)
        yellow = (H >= 15) & (H <= 35) & (S >= 40) & (V >= 250)

        white_runs = self._mask_runs(white, 15, x0)
        if white_runs:
            best = max(white_runs, key=lambda r: int(white[:, r[0] - x0:r[1] - x0].sum()))
            result["cursor_x"] = (best[0] + best[1]) // 2
        result["blue_regions"] = self._mask_runs(blue, 10, x0)
        result["yellow_regions"] = self._mask_runs(yellow, 10, x0)

        result["valid"] = result["cursor_x"] is not None and (
            len(result["blue_regions"]) > 0 or len(result["yellow_regions"]) > 0
        )

        print("Progress bar analysis result:", result)
        return result

    def _get_cursor_direction_from_frame(self, frame_count: int) -> int:
        """
        根据帧数计算游标方向
        游标从最左侧到最右侧需要88帧，然后反向
        
        Args:
            frame_count: 当前帧数
            
        Returns:
            int: 1=向右，-1=向左
        """
        # 计算当前在第几个周期内
        cycle_frame = frame_count % 176  # 一个完整周期是176帧（右行88+左行88）
        # 0-87帧向右，88-175帧向左
        return 1 if cycle_frame < 88 else -1

    def _calculate_blue_region_zero_frame(self, blue_regions: List[Tuple[int, int]]) -> Optional[int]:
        """计算蓝色区域多少帧后会收缩归0"""
        if len(blue_regions) == 0:
            return None
        all_starts = [start for start, end in blue_regions]
        all_ends = [end for start, end in blue_regions]
        leftmost = min(all_starts)
        rightmost = max(all_ends)
        blue_center = (leftmost + rightmost) / 2
        distance_to_center = abs(rightmost - blue_center)
        frames_to_zero = distance_to_center / BLUE_ZONE_SHRINK_PX_PER_FRAME
        return int(frames_to_zero)

    def _calculate_click_timing(
        self,
        cursor_x: int,
        yellow_regions: List[Tuple[int, int]],
        current_frame: int,
    ) -> Optional[float]:
        """计算游标到达黄色区域的最佳点击时机
        
        Args:
            cursor_x: 当前游标 X 坐标
            yellow_regions: 黄色区域列表 [(start, end), ...]
            current_frame: 当前帧数
        
        Returns:
            float or None: 应该等待的秒数，None 表示无法/不应该点击
        """
        if len(yellow_regions) == 0:
            return None
        
        bar_left = self.coords.progress_bar_left
        bar_right = self.coords.progress_bar_right
        
        # 取黄色区域最靠近游标的一侧作为目标
        yellow_start, yellow_end = yellow_regions[0]
        
        # 根据当前帧数计算游标方向
        cursor_direction = self._get_cursor_direction_from_frame(current_frame)
        
        target_x = yellow_start if cursor_direction > 0 else yellow_end
        
        # 计算距离（考虑方向）
        distance = target_x - cursor_x
        
        # 判断是否需要等待反弹
        # 如果游标向右移动但目标在左边，或游标向左移动但目标在右边
        # 需要计算反弹后的距离
        if cursor_direction > 0 and distance < 0:
            # 游标向右，目标在左边 -> 需要先到右边界反弹
            distance_to_right = bar_right - cursor_x
            distance_back = bar_right - target_x
            total_distance = distance_to_right + distance_back
        elif cursor_direction < 0 and distance > 0:
            # 游标向左，目标在右边 -> 需要先到左边界反弹
            distance_to_left = cursor_x - bar_left
            distance_back = target_x - bar_left
            total_distance = distance_to_left + distance_back
        else:
            # 游标正在向目标移动
            total_distance = abs(distance)
        
        # 计算需要的帧数和时间
        frames_needed = total_distance / CURSOR_SPEED_PX_PER_FRAME
        time_needed = frames_needed / 60.0  # 假设 60 FPS
        
        # 如果时间太长（超过5秒），可能计算有误或游戏状态变化
        if time_needed > 5.0:
            return None
        
        return time_needed

    def _calculate_blue_click_timing(
        self,
        cursor_x: int,
        blue_regions: List[Tuple[int, int]],
        current_frame: int,
    ) -> Optional[float]:
        """计算游标到达蓝色区域的最佳点击时机
        考虑蓝色区域会向中心收缩
        
        Args:
            cursor_x: 当前游标 X 坐标
            blue_regions: 蓝色区域列表 [(start, end), ...]
            current_frame: 当前帧数
        
        Returns:
            float or None: 应该等待的秒数，None 表示无法/不应该点击
        """
        if len(blue_regions) == 0:
            return None
        
        # 合并所有蓝色区域，找到最左侧和最右侧
        all_starts = [start for start, end in blue_regions]
        all_ends = [end for start, end in blue_regions]
        blue_start = min(all_starts)
        blue_end = max(all_ends)
        
        # 计算蓝色区域的中心位置
        blue_center = (blue_start + blue_end) / 2
        
        # 根据当前帧数计算游标方向
        cursor_direction = self._get_cursor_direction_from_frame(current_frame)
        
        # 计算游标到达当前蓝色区域中心的距离和时间
        distance = blue_center - cursor_x
        
        # 游标正在向目标移动
        total_distance = abs(distance)
        
        # 计算需要的帧数
        frames_needed = total_distance / CURSOR_SPEED_PX_PER_FRAME
        
        # 计算在这段时间内，蓝色区域会收缩多少
        # 蓝色区域从两端向中心收缩，每帧收缩 BLUE_ZONE_SHRINK_PX_PER_FRAME 像素
        # 假设蓝色区域的左边界向右移动，右边界向左移动，各收缩一半
        shrink_distance = BLUE_ZONE_SHRINK_PX_PER_FRAME * frames_needed
        
        # 预测到达时蓝色区域的新位置
        predicted_blue_start = blue_start + shrink_distance
        predicted_blue_end = blue_end - shrink_distance
        
        # 检查预测的蓝色区域是否还有效（宽度大于10像素）
        if predicted_blue_end - predicted_blue_start < 5:
            return None  # 区域太小，无法点击
        
        # 转换为时间
        time_needed = frames_needed / 60.0  # 假设 60 FPS
        
        # 如果时间太长（超过5秒），可能计算有误或游戏状态变化
        if time_needed > 5.0:
            return None
        
        return time_needed

    # ============ Game flow ============
    # 抛竿蓄力环几何:按钮盒 [1057,483,162,175] → 圆心/半径按 1080p 实测
    CAST_RING_CENTER = (1138, 570)
    CAST_RING_R = (66, 94)

    def hold_cast_until_green(self, max_hold: float = 5.0) -> bool:
        """按住抛竿按钮蓄力,蓄力环变绿的瞬间松手(Perfect Cast)。

        绿色只在按住蓄力期间出现(空闲时环上只有白色弧,已用调试帧确认),
        所以必须 touch_down 持续按住、边按边检测、见绿 touch_up。
        超时也松手(等效普通抛竿,不会更差)。
        """
        print("  按住抛竿蓄力,等待变绿...")
        cx, cy = self.CAST_RING_CENTER
        r_in, r_out = self.CAST_RING_R
        x0, y0 = cx - r_out, cy - r_out
        size = r_out * 2
        yy, xx = np.mgrid[0:size, 0:size]
        rr2 = (xx - r_out) ** 2 + (yy - r_out) ** 2
        annulus = (rr2 >= r_in * r_in) & (rr2 <= r_out * r_out)

        self.controller.post_touch_down(*self.coords.cast_rod).wait()
        t0 = time.time()
        best_seen = 0
        self.last_hold_best_green = 0
        released_green = False
        try:
            while self.running and not self.context.tasker.stopping:
                shot = self.get_screenshot()
                if shot is None:
                    continue
                img = np.asarray(shot)
                patch = img[y0:y0 + size, x0:x0 + size, :3]
                if patch.shape[0] != size or patch.shape[1] != size:
                    print("    ⚠️ 截图尺寸异常,直接松手")
                    break
                H, S, V = self._bgr_to_hsv(patch)
                green = (H >= 35) & (H <= 85) & (S >= 80) & (V >= 100) & annulus
                n = int(green.sum())
                best_seen = max(best_seen, n)
                if n >= 40:
                    released_green = True
                    break
                if time.time() - t0 > max_hold:
                    try:
                        from PIL import Image
                        dbg = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "debug", "fishing_cast_debug.png")
                        Image.fromarray(img[..., :3][..., ::-1]).save(dbg)
                        print(f"    🐞 已保存蓄力中调试帧: {dbg}")
                    except Exception as e:
                        print("    🐞 调试帧保存失败:", e)
                    print(f"    ⏳ 蓄力 {max_hold}s 未见变绿(峰值绿像素 {best_seen}),直接松手")
                    break
        finally:
            self.controller.post_touch_up().wait()
        self.last_hold_best_green = best_seen
        if released_green:
            print(f"    🟢 蓄力环变绿(绿像素 {n},耗时 {time.time()-t0:.2f}s),松手抛竿!")
        return released_green

    def wait_for_fish(self) -> Tuple[bool, bool]:
        print("  等待鱼上钩...")
        start_time = time.time()
        while self.running and not self.context.tasker.stopping:
            screenshot = self.get_screenshot()
            if screenshot is None:
                continue
            if self.detect_exclamation(screenshot):
                print("  鱼上钩! 感叹号出现")
                return True, False
            if time.time() - start_time > 25:
                return False, True
            self.delay(self.timing.wait_fish_interval)
        return False, True

    def play_minigame(self) -> bool:
        """玩钓鱼小游戏 - 预测式策略
        
        策略：
        1. 截图分析游标和区域位置
        2. 计算到达黄色/蓝色区域的时间
        3. 等待到最佳时机后点击
        4. 点击后游标重置，重复步骤1
        """
        print("  开始小游戏（预测式策略）...")
        start_time = time.time()
        click_count = 0
        total_time = 17  # 默认总时间，后续从识别结果更新
        seen_valid = False   # 是否已经见过有效的进度条
        dumped_debug = False  # 是否已保存过调试帧
        

        while self.running and not self.context.tasker.stopping:
            current_time = time.time()
            frame = int((current_time - start_time) * 60)

            screenshot = self.get_screenshot()
            cap_cost = time.time() - current_time
            # if total_time is None:
            #     result = self.context.run_recognition("Reco_Minigame_Total_Time", screenshot)
            #     total_time = int(result.best_result.text)
            #     print("小游戏⏲️总时间识别结果:", total_time)
            
            # 超时检查
            if current_time - start_time > total_time:
                return True if click_count > 0 else False
            
            # 分析进度条
            bar_info = self.analyze_progress_bar(screenshot)
            if not bar_info["valid"]:
                if seen_valid:
                    return True  # 进度条消失,小游戏结束(可能已经钓到)
                # 开局阶段:进度条可能尚未渲染,等待其出现(此前分析链路慢,
                # 无意中起到了等待作用;提速后必须显式等待)
                if not dumped_debug:
                    try:
                        from PIL import Image
                        dbg = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "debug", "fishing_minigame_debug.png")
                        Image.fromarray(np.asarray(screenshot)[..., :3][..., ::-1]).save(dbg)
                        print(f"    🐞 已保存小游戏调试帧: {dbg}")
                    except Exception as e:
                        print("    🐞 调试帧保存失败:", e)
                    dumped_debug = True
                if current_time - start_time < 4.0:
                    self.delay(0.15)
                    continue
                print("  ⚠️ 4秒内未检测到进度条,视为本轮失败")
                return False
            seen_valid = True
            
            cursor_x = bar_info["cursor_x"]
            yellow_regions = bar_info["yellow_regions"]
            blue_regions = bar_info["blue_regions"]
            
            # 计算蓝色区域归0时间
            frames_to_zero = self._calculate_blue_region_zero_frame(blue_regions)
            blue_region_zero_time = frames_to_zero / 60.0 if frames_to_zero is not None else None
            
            # 2. 选择点击策略：优先黄色，其次蓝色
            target_zone = None
            wait_time = None
            
            # 检查是否应该点击黄色区域
            should_click_yellow = False
            if len(yellow_regions) > 0:
                # 检查游标是否已经越过所有黄色区域（在最后一个黄色区域的右侧）
                last_yellow_end = yellow_regions[-1][1]
                cursor_direction = self._get_cursor_direction_from_frame(frame)
                if cursor_x + CURSOR_SPEED_PX_PER_FRAME * 0.27 * 60 < last_yellow_end:
                    should_click_yellow = True
            
            if should_click_yellow:
                # 尝试点击黄色区域（暴击）
                wait_time = self._calculate_click_timing(cursor_x, yellow_regions, frame)
                # 检查是否在蓝色区域归0前能点击
                if wait_time is not None:
                    if blue_region_zero_time is None or wait_time + 0.27 < blue_region_zero_time:
                        target_zone = "yellow"
                    else:
                        wait_time = None  # 超时，无法点击
            
            # 如果无法点击黄色区域，尝试蓝色区域
            if target_zone is None and len(blue_regions) > 0:
                wait_time = self._calculate_blue_click_timing(cursor_x, blue_regions, frame)
                # 检查是否在蓝色区域归0前能点击
                if wait_time is not None:
                    if blue_region_zero_time is None or wait_time + 0.27 < blue_region_zero_time:
                        target_zone = "blue"
                    else:
                        wait_time = None  # 超时，无法点击
            
            # 如果两个区域都无法点击，等待蓝色区域归0后重置
            if target_zone is None:
                if blue_region_zero_time is not None and blue_region_zero_time > 0:
                    print(f"    ⏳ 无可点击区域，等待 {blue_region_zero_time:.2f}s 后蓝色区域归0")
                    self.delay(blue_region_zero_time)
                    start_time = time.time()
                    continue
                else:
                    print("    ⚠️ 未检测到有效区域，等待...")
                    continue

            now = time.time()
            elapsed = now - current_time

            print("分析耗时: {:.3f}s (其中截图 {:.3f}s)".format(elapsed, cap_cost))
            
            # 3. 等待到最佳时机（提前补偿输入延迟） 点击后有7帧延迟，点击动作需要约0.055s 
            adjusted_wait = wait_time - elapsed - 0.045 
            
            if adjusted_wait > 0:
                zone_name = "黄色区" if target_zone == "yellow" else "蓝色区"
                print(f"    ⏱️ 预测 {wait_time:.3f}s 后到达{zone_name} (等待 {adjusted_wait:.3f}s)")
                self.delay(adjusted_wait)
            elif adjusted_wait < -0.08:
                # 分析结束时预测时机已明显过期,此时点击必然脱靶,等游标下一轮
                print(f"    ⏭️ 时机已错过 {-adjusted_wait:.3f}s,跳过本次点击等下一轮")
                continue
            else:
                print(f"    ⚡ 立即点击 (预测时间: {wait_time:.3f}s)")
            
            # 4. 点击！
            self.tap(*self.coords.cast_rod)
            click_count += 1
            
            zone_emoji = "🟡" if target_zone == "yellow" else "🔵"
            zone_name = "暴击区" if target_zone == "yellow" else "蓝色区"
            cursor_direction = self._get_cursor_direction_from_frame(frame)
            print(f"    {zone_emoji} 点击{zone_name}! (游标: {cursor_x}, 帧: {frame}, 方向: {'→' if cursor_direction > 0 else '←'})")
            
            # 5. 点击后短暂等待，让游标重置到最左边
            self.delay(0.6)  # 等待游标重置
            start_time = time.time()  # 重置开始时间
        
        return False

    def sell_all_fish(self):
        print("\n==================================================")
        print("🐟💰 开始卖鱼...")

        # Use pipeline to execute sell sequence
        detail = self.context.run_task("SellFish_Start")

        if detail and detail.nodes:
            self.total_sell_count += 1
            self.fish_since_last_sell = 0
            print(f"✅ 卖鱼完成 (第 {self.total_sell_count} 次)")
        else:
            # 入口未识别到(如卖鱼图标未出现),不清计数,下一条鱼后自动重试
            print("⚠️ 卖鱼失败:未能进入卖鱼界面,下一条鱼后重试")
        print("==================================================\n")
        self.delay(1.0)

    def check_and_sell_fish(self):
        if self.fish_since_last_sell >= self.sell_interval:
            print(f"\n📦 已成功钓到 {self.fish_since_last_sell} 条鱼，触发自动卖鱼")
            self.sell_all_fish()

    def main_loop(self) -> bool:
        self.fish_count += 1
        print(f"\n[第 {self.fish_count} 次钓鱼]")
        
        # 运行 Casting_Rod pipeline:base(安卓)资源为直接按压抛竿;
        # PlayCover 资源层将其覆盖为 Custom(HoldCastGreen),按住蓄力变绿再松手
        casting_result = self.context.run_task("Casting_Rod")

        # print("task Casting_Rod result:", casting_result)
        
        # 检查是否检测到鱼上钩
        if not casting_result or not casting_result.nodes[-1].action.success:
            print("  等待鱼上钩超时或未检测到，重试")
            return False
        
        print("  鱼上钩! 进入小游戏...")
        self.delay(self.timing.after_cast)

        success = self.play_minigame()
        if success:
            self.success_count += 1
            self.fish_since_last_sell += 1
            print(f"  ✅ 钓鱼成功 (累计成功 {self.success_count})")
        else:
            print("  ❌ 钓鱼失败")

        # 结算
        self.delay(self.timing.after_catch)
        print("  点击结算...")
        self.tap(*self.coords.screen_center)
        self.delay(1.0)

        self.check_and_sell_fish()

        return success

    def run(self, max_count: Optional[int] = None) -> bool:
        self.running = True
        self.fish_count = 0
        self.success_count = 0
        print("==================================================")
        print("🎣 自动钓鱼开始 (custom action)")
        print(f"最大次数: {max_count if max_count else '无限'}")
        print("==================================================")

        try:
            while self.running and not self.context.tasker.stopping:
                if max_count and self.fish_count >= max_count:
                    break
                self.main_loop()
        finally:
            self.running = False
            # 兜底：结束后卖出剩余鱼获
            if self.fish_since_last_sell > 0:
                print(f"\n📦 钓鱼结束，卖出剩余 {self.fish_since_last_sell} 条鱼")
                self.sell_all_fish()
        return self.success_count > 0


@AgentServer.custom_action("FishingAction")
class FishingAction(CustomAction):
    """Entry point for Maa pipeline custom action."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        import json
        
        # Access custom_action_param from pipeline JSON
        # It's a JSON string that needs to be parsed
        param_str = getattr(argv, 'custom_action_param', '{}')
        print("FishingAction parameters (raw):", param_str)
        param = json.loads(param_str) if isinstance(param_str, str) else param_str
        
        max_count = int(param.get("max_count", 1))
        # 鱼包容量 30;留 5 条余量,防止开局未能清包时计数与实际背包脱节导致包满卡死
        sell_interval = int(param.get("sell_interval", 25))

        bot = FishingBot(
            context=context,
            sell_interval=sell_interval
        )
        return bot.run(max_count=max_count)


@AgentServer.custom_action("HoldCastGreen")
class HoldCastGreenAction(CustomAction):
    """按住抛竿蓄力,蓄力环变绿瞬间松手(Perfect Cast)。

    仅由 PlayCover 资源层覆盖后的 Casting_Rod 节点以 Custom 动作调用;
    base(安卓)资源不经过此路径,保持原有直接按压行为不变。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        bot = FishingBot(context=context)
        bot.running = True
        # 结算转场未结束时按压可能不被游戏接收(表现为全程 0 绿像素),
        # 此时线并未抛出,重试按压是安全的;若已蓄力(绿像素>0)则不重试
        for attempt in range(3):
            if bot.hold_cast_until_green():
                return True
            if bot.last_hold_best_green > 0:
                return True  # 蓄力发生过,线已抛出(只是未达绿色阈值),不可重按
            if attempt < 2:
                print(f"    🔁 按压似乎未被游戏接收,1s 后重试(第 {attempt + 2} 次)")
                bot.delay(1.0)
        return True
