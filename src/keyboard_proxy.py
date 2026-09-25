#!/usr/bin/python3
"""
Keyboard Proxy - キーボードプロキシ
====================================

このモジュールは、複数のUSBキーボードデバイスからの入力を受け取り、
USB HIDガジェットデバイスに転送するプロキシサービスを提供します。

主な機能:
- 複数キーボードの同時管理
- USキー配列からJIS配列への自動リマップ
- GPIOボタンによる特殊機能（メール入力、シャットダウン等）
- Pimoroni Keybow Mini の APA102 LED による状態表示 (SPI 経由)

動作環境:
- Raspberry Pi（USB HIDガジェットモードが有効な環境）
- Python 3.7以降
- evdevライブラリ
- gpiozeroライブラリ（GPIO機能用、オプション）
- python3-spidev（LED機能用、オプション。SPI 有効化が必要）

使用方法:
    直接実行:
        python3 keyboard_proxy.py
    
    systemdサービスとして:
        sudo systemctl start keyboard-proxy.service
"""

import logging
import asyncio
import signal
import re
import threading
import time
import evdev
from evdev import InputDevice, ecodes
from hid_keys import hid_key_map as hid_keys
import proxy_core

# =============================================================================
# 初期化処理
# =============================================================================
# 設定ファイルを読み込み、ロギングを設定
CONFIG = proxy_core.load_config()
proxy_core.setup_logging(CONFIG)

# リマップ機能の有効/無効フラグ（グローバル変数）
# GPIOボタンまたは他の方法でトグル可能
REMAP_ENABLED = True

# =============================================================================
# GPIOライブラリのインポート（オプション）
# =============================================================================
# gpiozeroライブラリが利用できない環境（非Raspberry Pi）でも
# プロキシの基本機能は動作するように、フォールバッククラスを定義
try:
    from gpiozero import Button
except ImportError:
    logging.warning("gpiozero library not found. GPIO button functions disabled.")
    # ダミーのButtonクラス - すべての操作を無視
    class Button:
        def __init__(self, *args, **kwargs): pass
        def __getattr__(self, name): return lambda *args, **kwargs: None

# =============================================================================
# SPI ライブラリのインポート (Keybow Mini の APA102 LED 駆動用、オプション)
# =============================================================================
# Keybow Mini は APA102 RGB LED を SPI 経由で駆動する。
# spidev が無い環境 (非 Raspberry Pi、SPI 未有効) でもプロキシ本体は動作する。
try:
    import spidev
    SPI_AVAILABLE = True
except ImportError:
    logging.warning("spidev library not found. LED functions disabled.")
    SPI_AVAILABLE = False


# =============================================================================
# LED ステータスマネージャ (Pimoroni Keybow Mini / APA102)
# =============================================================================
class LedStatusManager:
    """
    Pimoroni Keybow Mini の APA102 RGB LED を制御するクラス。

    APA102 プロトコルを spidev で直接叩く実装 (外部 LED ライブラリ非依存):
      - Start frame: 0x00 を 4 バイト
      - LED frame:   各 LED ごと 4 バイト [0xE0|brightness5bit, B, G, R]
      - End frame:   0xFF を ceil(num_leds/16) バイト以上

    リマップ機能 (US→JIS) の有効/無効状態を色で表示する。
    主機能 (キーボードプロキシ) を絶対にブロックしないことを最優先とし、
    依存欠如・設定 disabled・ハード初期化失敗のいずれの場合も
    例外を投げず、ログを残してフォールバックする。
    """

    def __init__(self, led_settings):
        """
        Args:
            led_settings (dict): config.json の led_settings ブロック
        """
        self.enabled = False
        self.spi = None
        self.num_leds = 0
        self.global_brightness = 31  # APA102 グローバル輝度 0-31
        self.colors = led_settings.get("colors", {}) if led_settings else {}
        self._boot_self_test = bool(led_settings.get("boot_self_test", True)) if led_settings else False
        # ブートシーケンス用スレッドと GPIO コールバックスレッドからの
        # 同時 xfer2 を防ぐためのロック。
        self._spi_lock = threading.Lock()

        if not led_settings or not led_settings.get("enabled", False):
            logging.info("LED disabled by config.")
            return

        if not SPI_AVAILABLE:
            logging.warning(
                "LED enabled in config but python3-spidev not installed; "
                "skipping LED initialization."
            )
            return

        try:
            self.num_leds = int(led_settings.get("led_count", 3))
            spi_bus = int(led_settings.get("spi_bus", 0))
            spi_device = int(led_settings.get("spi_device", 0))
            spi_hz = int(led_settings.get("spi_hz", 4000000))
            # APA102 のグローバル輝度フィールドは 5bit (0-31)。
            # config 側は 0-255 で書けるよう 8bit→5bit にマッピング。
            # brightness=0 は意図的な完全消灯として 0/31 を許容する。
            # 1 以上の場合は丸めで 0/31 にならないよう最低 1/31 を保証する。
            br_8bit = max(0, min(255, int(led_settings.get("brightness", 1))))
            if br_8bit == 0:
                self.global_brightness = 0
            else:
                self.global_brightness = max(1, br_8bit * 31 // 255)

            self.spi = spidev.SpiDev()
            self.spi.open(spi_bus, spi_device)
            self.spi.max_speed_hz = spi_hz
            self.spi.mode = 0b00

            self.enabled = True
            logging.info(
                "LED hardware initialized: %d APA102 LEDs on spidev%d.%d "
                "(spi_hz=%d, global_brightness=%d/31)",
                self.num_leds, spi_bus, spi_device, spi_hz, self.global_brightness,
            )

            # 起動セルフテストは run_boot_sequence() で別スレッド実行する。
            # __init__ では走らせない (呼び出し側が初期状態と順序を決められるように)。
        except Exception as e:
            logging.error("LED init failed: %s", e)
            self.enabled = False
            if self.spi is not None:
                try:
                    self.spi.close()
                except Exception:
                    pass
            self.spi = None

    def _self_test(self):
        """起動セルフテスト: 赤→緑→青→消灯。"""
        for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
            self._fill(rgb)
            time.sleep(0.4)
        self._fill((0, 0, 0))
        logging.info("LED self-test complete.")

    def run_boot_sequence(self, final_state_callable=None):
        """
        起動シーケンスを別スレッドで実行する (呼び出しは即座に返る)。

        セルフテスト (`boot_self_test=true` の場合) を走らせ、その完了後に
        `final_state_callable` を呼んで通常状態の色を表示する。これにより:

        - 呼び出し側 (KeyBowManager.__init__) と asyncio イベントループ起動を
          ブロックしない (Copilot レビュー指摘 #2 への対応)
        - セルフテスト中の途中状態と通常状態が競合しないよう順序を保証する

        セルフテストが無効・LED 無効・SPI 不在の場合は、`final_state_callable`
        があればその場で同期実行する (スレッドを起こさない)。
        """
        if not self.enabled:
            if final_state_callable:
                try:
                    final_state_callable()
                except Exception as e:
                    logging.error("Final state callable failed: %s", e)
            return

        if not self._boot_self_test:
            if final_state_callable:
                try:
                    final_state_callable()
                except Exception as e:
                    logging.error("Final state callable failed: %s", e)
            return

        def _runner():
            try:
                self._self_test()
            except Exception as e:
                logging.error("Self-test failed: %s", e)
            if final_state_callable:
                try:
                    final_state_callable()
                except Exception as e:
                    logging.error("Final state callable failed: %s", e)

        threading.Thread(target=_runner, name="LedBootSequence", daemon=True).start()

    def _fill(self, rgb):
        """全 LED を rgb (r,g,b) で塗る。スレッドセーフ。"""
        if not self.enabled or self.spi is None:
            return
        try:
            r = max(0, min(255, int(rgb[0])))
            g = max(0, min(255, int(rgb[1])))
            b = max(0, min(255, int(rgb[2])))
            led_header = 0xE0 | (self.global_brightness & 0x1F)
            # APA102 ワイヤフォーマット: start + (header,B,G,R) * N + end
            data = [0x00] * 4
            for _ in range(self.num_leds):
                data += [led_header, b, g, r]
            # end frame: 各 LED 後の clock latching に必要 (ceil(N/16) バイト以上)
            data += [0xFF] * ((self.num_leds + 15) // 16)
            with self._spi_lock:
                self.spi.xfer2(data)
        except Exception as e:
            logging.error("LED fill failed (rgb=%s): %s", tuple(rgb), e)

    def show_remap_state(self, remap_enabled):
        """リマップ状態を LED 色で表示する。"""
        if not self.enabled:
            return
        key = "remap_enabled" if remap_enabled else "remap_disabled"
        default = [0, 255, 0] if remap_enabled else [255, 0, 0]
        rgb = self.colors.get(key, default)
        self._fill(tuple(rgb))
        logging.info("LED state: %s rgb=%s", key, tuple(rgb))


class KeyboardProxy:
    """
    キーボードプロキシクラス
    
    1つの入力デバイス（キーボード）からイベントを受け取り、
    USB HIDガジェットデバイスに転送します。
    キー配列のリマップ機能も提供します。
    
    Attributes:
        input_device_path (str): 入力デバイスのパス（例: /dev/input/event0）
        hid_output_path (str): HID出力デバイスのパス（例: /dev/hidg0）
        device (evdev.InputDevice): 入力デバイスオブジェクト
        modifier (int): 現在押されているモディファイアキーのビットマスク
        pressed_keys (set): 現在押されているキーのセット
    """
    
    def __init__(self, input_device_path, hid_output_path, loop):
        """
        キーボードプロキシを初期化します。
        
        Args:
            input_device_path (str): 入力デバイスのパス
            hid_output_path (str): HID出力デバイスのパス
            loop (asyncio.AbstractEventLoop): 非同期イベントループ
        """
        # インスタンス固有のロガーを作成（デバイスパスをサフィックスに使用）
        self.log = logging.getLogger(f"KeyboardProxy-{input_device_path.split('/')[-1]}")
        self.loop = loop
        self.input_device_path = input_device_path
        self.hid_output_path = hid_output_path
        self.device = None
        
        # モディファイアキーとビット位置のマッピング
        # HIDレポートの最初のバイトは8ビットのモディファイアビットマスク:
        #   bit 0: 左Ctrl,  bit 1: 左Shift, bit 2: 左Alt,  bit 3: 左Meta(Win)
        #   bit 4: 右Ctrl,  bit 5: 右Shift, bit 6: 右Alt,  bit 7: 右Meta(Win)
        self.modifiers_map = {
            'KEY_LEFTCTRL': 0, 'KEY_LEFTSHIFT': 1, 'KEY_LEFTALT': 2, 'KEY_LEFTMETA': 3, 
            'KEY_RIGHTCTRL': 4, 'KEY_RIGHTSHIFT': 5, 'KEY_RIGHTALT': 6, 'KEY_RIGHTMETA': 7
        }
        
        # 状態をリセット
        self.reset_state()

    def connect_device(self):
        """
        入力デバイスに接続し、排他的にキャプチャします。
        
        Returns:
            bool: 接続に成功した場合はTrue、失敗した場合はFalse
        
        Note:
            grab()によりデバイスを排他的に取得するため、
            他のプロセス（Xサーバーなど）はこのキーボードからの入力を受け取りません。
        """
        try:
            self.device = InputDevice(self.input_device_path)
            # デバイスを排他的に取得（他のプロセスからのアクセスをブロック）
            self.device.grab()
            self.log.info(f"Keyboard captured: {self.device.path} ({self.device.name}) -> {self.hid_output_path}")
            return True
        except Exception as e:
            self.log.error(f"Failed to connect to {self.input_device_path}: {e}")
            return False

    def reset_state(self):
        """
        キーボードの内部状態をリセットします。
        
        デバイス再接続時やエラー発生時に呼び出し、
        押されっぱなしのキーやモディファイアをクリアします。
        """
        self.modifier = 0b00000000    # モディファイアビットマスク
        self.pressed_keys = set()      # 押下中の通常キー
        self.is_shift_up = False       # Shiftを一時的に押す必要があるフラグ
        self.is_shift_down = False     # Shiftを一時的に離す必要があるフラグ
        self.shift_bit = 0b00100010    # 左右Shiftのビットマスク（bit 1 と bit 5）

    async def run(self):
        """
        プロキシのメインループを実行します。
        
        入力デバイスからのイベントを継続的に読み取り、処理します。
        デバイスが切断された場合は再接続を試みます。
        """
        while True:
            # デバイス未接続または接続失敗時は5秒待機して再試行
            if not self.device and not self.connect_device():
                await asyncio.sleep(5)
                continue
            try:
                while True:
                    # 非同期でイベントを読み取り
                    # run_in_executor を使用してブロッキング読み取りを非同期化
                    event = await self.loop.run_in_executor(None, self.device.read_one)
                    if event is None:
                        # イベントがない場合は短時間待機
                        await asyncio.sleep(0.01)
                        continue
                    # イベントを処理
                    self.process_event(event)
            except (OSError, asyncio.CancelledError) as e:
                # デバイス切断またはタスクキャンセル
                self.log.error(f"Keyboard {self.input_device_path} disconnected: {type(e).__name__}")
                if self.device:
                    self.device.close()
                self.device = None
                break
            except Exception as e:
                self.log.error(f"Unexpected error: {e}", exc_info=True)
                break

    def process_event(self, event):
        """
        入力イベントを処理します。
        
        キーイベント（EV_KEY）のみを処理し、他のイベントタイプは無視します。
        
        Args:
            event (evdev.InputEvent): 処理するイベント
        """
        # キーイベント以外は無視
        if event.type != ecodes.EV_KEY:
            return

        try:
            # イベントコードからキー名を取得
            keycode = ecodes.KEY[event.code]
        except (IndexError, KeyError):
            self.log.debug(f"Ignoring unknown keycode: {event.code}")
            return
        
        # 一部のキーコードはリスト（複数のエイリアス）として返される
        if isinstance(keycode, list):
            keycode = keycode[0]

        # キーの状態: 0=リリース, 1=プレス, 2=リピート
        keystate = event.value

        # モディファイアキーは別処理
        if keycode in self.modifiers_map:
            self.update_modifier(keycode, keystate)
        elif keystate == 0:  # キーリリース
            self.release(keycode)
        elif keystate == 1 or keystate == 2:  # キープレスまたはリピート
            self.press(keycode)

    def update_modifier(self, keycode, keystate):
        """
        モディファイアキーの状態を更新します。
        
        Args:
            keycode (str): モディファイアキーのキーコード
            keystate (int): キーの状態（0=リリース, 1=プレス, 2=リピート）
        """
        if keystate == 0:
            # キーリリース: 対応するビットをクリア
            self.modifier &= ~(1 << self.modifiers_map[keycode])
        else:
            # キープレス: 対応するビットをセット
            self.modifier |= (1 << self.modifiers_map[keycode])
        # HIDレポートを送信
        self.update_state()

    def release(self, keycode):
        """
        通常キーのリリースを処理します。
        
        Args:
            keycode (str): リリースされたキーのキーコード
        """
        if keycode in self.pressed_keys:
            self.pressed_keys.remove(keycode)
            self.update_state()

    def press(self, keycode):
        """
        通常キーのプレスを処理します。
        
        Args:
            keycode (str): プレスされたキーのキーコード
        """
        if keycode not in self.pressed_keys:
            self.pressed_keys.add(keycode)
            self.update_state()

    def remap(self, keycode):
        """
        キーコードをリマップ（USキー配列からJIS配列へ変換）します。
        
        REMAP_ENABLEDがFalseの場合、リマップなしでそのまま返します。
        
        主なリマップ:
        - [ -> ]
        - ] -> \
        - Shift+7 -> Shift+6 (&を^に)
        - Shift+8 -> ' (アスタリスクをアポストロフィに)
        - など
        
        Args:
            keycode (str): リマップ前のキーコード
        
        Returns:
            int: リマップ後のHIDキーコード
        """
        global REMAP_ENABLED
        if not REMAP_ENABLED:
            # リマップ無効時はそのまま変換
            return hid_keys.get(keycode, 0)
        if keycode not in hid_keys: 
            return 0
        
        # === 基本的なキーリマップ ===
        # 左角括弧 -> 右角括弧
        if keycode == 'KEY_LEFTBRACE': 
            keycode = 'KEY_RIGHTBRACE'
        # 右角括弧 -> バックスラッシュ
        elif keycode == 'KEY_RIGHTBRACE': 
            keycode = 'KEY_BACKSLASH'
        # === Shiftキーが押されている場合のリマップ ===
        elif self.modifier & self.shift_bit:
            if keycode == 'KEY_7': keycode = 'KEY_6'        # Shift+7(&) -> Shift+6(^)
            elif keycode == 'KEY_8': keycode = 'KEY_APOSTROPHE'  # Shift+8(*) -> '
            elif keycode == 'KEY_9': keycode = 'KEY_8'      # Shift+9(() -> Shift+8(*)
            elif keycode == 'KEY_0': keycode = 'KEY_9'      # Shift+0()) -> Shift+9(()
            elif keycode == 'KEY_EQUAL': keycode = 'KEY_SEMICOLON'  # Shift+=(+) -> Shift+;(:)
            elif keycode == 'KEY_GRAVE': keycode = 'KEY_EQUAL'     # Shift+`(~) -> Shift+=(+)
            elif keycode == 'KEY_MINUS': keycode = 'KEY_RO'        # Shift+-(_) -> _（JIS配列）
            elif keycode == 'KEY_2': keycode = 'KEY_LEFTBRACE'; self.is_shift_down = True  # Shift+2(@) -> [
            elif keycode == 'KEY_6': keycode = 'KEY_EQUAL'; self.is_shift_down = True      # Shift+6(^) -> =
            elif keycode == 'KEY_BACKSLASH': keycode = 'KEY_YEN'    # Shift+\(|) -> |（JIS配列）
            elif keycode == 'KEY_SEMICOLON': keycode = 'KEY_APOSTROPHE'; self.is_shift_down = True  # Shift+;(:) -> '
            elif keycode == 'KEY_APOSTROPHE': keycode = 'KEY_2'     # Shift+'(") -> Shift+2(@)
        # === Shiftキーが押されていない場合のリマップ ===
        else:
            if keycode == 'KEY_APOSTROPHE': keycode = 'KEY_7'; self.is_shift_up = True  # '(') -> Shift+7(&)
            elif keycode == 'KEY_GRAVE': keycode = 'KEY_LEFTBRACE'; self.is_shift_up = True  # `(`) -> Shift+[({)
            elif keycode == 'KEY_EQUAL': keycode = 'KEY_MINUS'; self.is_shift_up = True  # =(=) -> Shift+-(_)
            elif keycode == 'KEY_BACKSLASH': keycode = 'KEY_RO'  # \(\) -> \（JIS配列）
            
        return hid_keys.get(keycode, 0)

    def update_state(self):
        """
        現在のキー状態からHIDレポートを生成し、送信します。
        
        HIDキーボードレポートの構造（8バイト）:
        - byte 0: モディファイアビットマスク
        - byte 1: 予約（常に0）
        - bytes 2-7: 押されているキーのHIDコード（最大6キー）
        """
        # リマップ用フラグをリセット
        self.is_shift_up = False
        self.is_shift_down = False
        
        # 8バイトのHIDレポートを初期化
        report = bytearray(8)
        
        # 押されているキーをHIDコードに変換
        pressed_hid_codes = [self.remap(k) for k in self.pressed_keys]
        modifier = self.modifier
        
        # Shiftを一時的に押す必要がある場合
        if self.is_shift_up:
            modifier |= 0x02  # 左Shiftビットをセット
            report[0] = 0x02
            self.write_report(bytes(report))  # Shiftのみのレポートを先に送信
        # Shiftを一時的に離す必要がある場合
        elif self.is_shift_down: 
            modifier &= ~self.shift_bit  # Shiftビットをクリア
            
        # モディファイアをセット
        report[0] = modifier
        
        # 押されているキー（最大6つ）をレポートに設定
        # 0以外のHIDコードのみをフィルタリング
        for i, code in enumerate(filter(None, pressed_hid_codes[:6])):
            report[2 + i] = code
        
        # レポートを送信
        self.write_report(bytes(report))

    def write_report(self, buffer):
        """
        HIDレポートをガジェットデバイスに書き込みます。
        
        Args:
            buffer (bytes): 送信する8バイトのHIDレポート
        
        Raises:
            OSError: デバイスへの書き込みに失敗した場合
        """
        try:
            with open(self.hid_output_path, 'rb+') as fd:
                fd.write(buffer)
        except BlockingIOError:
            # バッファがいっぱいの場合（通常は一時的な問題）
            self.log.warning(f"BlockingIOError on {self.hid_output_path}")
        except OSError as e:
            # デバイスエラー（切断など）
            self.log.error(f"OSError on {self.hid_output_path}: {e}")
            raise e


class KeyBowManager:
    """
    GPIOボタンマネージャークラス
    
    Raspberry Pi のGPIOピンに接続されたボタンを管理し、
    ボタン操作に応じた特殊機能を提供します。
    
    機能:
    - ボタン1: 短押し=Alt+A、長押し=リマップ切り替え
    - ボタン2: 短押し=Alt+Y
    - ボタン3: 短押し=スペース
    - ボタン1+2長押し: メールアドレス入力
    - ボタン1+3長押し: シャットダウン
    
    Attributes:
        keyboard_hid_path (str): キーボードHIDデバイスのパス
        email_address (str): 自動入力用のメールアドレス
        led (LedStatusManager): LED ステータス表示マネージャ
    """
    
    def __init__(self, loop):
        """
        GPIOボタンマネージャーを初期化します。
        
        Args:
            loop (asyncio.AbstractEventLoop): 非同期イベントループ
        """
        self.loop = loop
        
        # HIDパスの設定を取得
        hid_paths = CONFIG["hid_paths"]
        if "keyboard_outputs" in hid_paths:
            self.keyboard_hid_path = hid_paths["keyboard_outputs"][0]
        else:
            self.keyboard_hid_path = hid_paths.get("keyboard", "/dev/hidg0")
        
        # メールアドレスの取得
        self.email_address = CONFIG.get("email_address", "")
        
        # gpiozeroのButtonクラスにカスタム属性を追加
        Button.was_held = False
        
        # GPIO設定の取得
        gpio_settings = CONFIG.get("gpio_settings", {})
        hold_time = gpio_settings.get("hold_time", 1.5)           # 長押し判定時間
        bounce_time = gpio_settings.get("bounce_time", 0.05)      # チャタリング防止時間
        self.combination_check_delay = gpio_settings.get("combination_check_delay", 0.2)
        
        # ボタン状態の追跡用辞書
        # was_held: 長押しが発生したか
        # combination_detected: 組み合わせ押しが検出されたか
        self.button_states = {
            1: {"was_held": False, "combination_detected": False},
            2: {"was_held": False, "combination_detected": False},
            3: {"was_held": False, "combination_detected": False}
        }
        
        # === ボタン1の設定（GPIO 6）===
        self.btn1 = Button(6, hold_time=hold_time, bounce_time=bounce_time)
        self.btn1.when_held = self.held1     # 長押しコールバック
        self.btn1.when_released = self.released1  # リリースコールバック
        
        # === ボタン2の設定（GPIO 22）===
        self.btn2 = Button(22, hold_time=hold_time, bounce_time=bounce_time)
        self.btn2.when_held = self.held2
        self.btn2.when_released = self.released2
        
        # === ボタン3の設定（GPIO 17）===
        self.btn3 = Button(17, hold_time=hold_time, bounce_time=bounce_time)
        self.btn3.when_held = self.held3
        self.btn3.when_released = self.released3
        
        # === LED初期化 ===
        # LED 制御は LedStatusManager に委譲する。失敗しても主機能には影響しない。
        self.led = LedStatusManager(CONFIG.get("led_settings", {}))
        # 起動シーケンス: 別スレッドでセルフテスト → 完了後に初期状態を表示。
        # __init__ をブロックせず、セルフテストと初期状態の競合も防ぐ。
        self.led.run_boot_sequence(
            final_state_callable=lambda: self.led.show_remap_state(REMAP_ENABLED)
        )

        logging.info(f"KeyBow initialized. Hold time: {hold_time}s")

    async def send_key_combination(self, modifier_bits, key_code, send_alt_after=False):
        """
        キーの組み合わせをHIDデバイスに送信します。
        
        Args:
            modifier_bits (int): モディファイアビットマスク（例: 0x04 = Alt）
            key_code (int): HIDキーコード
            send_alt_after (bool): キー送信後にAlt単独を送信するか
        """
        try:
            # キープレスレポートを作成
            press_report = bytearray(8)
            press_report[0] = modifier_bits
            press_report[2] = key_code
            
            # キーリリースレポート（すべてゼロ）
            release_report = bytearray(8)

            with open(self.keyboard_hid_path, 'rb+') as fd:
                # キープレスを送信
                fd.write(bytes(press_report))
                await asyncio.sleep(0.01)  # 短い遅延
                # キーリリースを送信
                fd.write(bytes(release_report))
                await asyncio.sleep(0.01)

                # 追加のAlt送信が必要な場合
                if send_alt_after:
                    alt_only_press = bytearray(8)
                    alt_only_press[0] = 0x04  # Altのみ
                    alt_only_release = bytearray(8)

                    fd.write(bytes(alt_only_press))
                    await asyncio.sleep(0.01)
                    fd.write(bytes(alt_only_release))

        except Exception as e:
            logging.error(f"Error sending key combination: {e}")

    async def send_email_address(self):
        """
        設定されたメールアドレスを1文字ずつキー入力として送信します。
        
        各文字をHIDキーコードに変換し、適切なモディファイア（Shift）と
        組み合わせてレポートを送信します。
        """
        email = self.email_address
        logging.info(f"Typing email: {email}")
        
        try:
            with open(self.keyboard_hid_path, 'rb+') as fd:
                for char in email:
                    press_report = bytearray(8)
                    release_report = bytearray(8)
                    shift = False

                    # === 小文字アルファベット (a-z) ===
                    if 'a' <= char <= 'z':
                        key_name = f'KEY_{char.upper()}'
                        press_report[2] = hid_keys.get(key_name, 0)
                    # === 大文字アルファベット (A-Z) ===
                    elif 'A' <= char <= 'Z':
                        shift = True
                        key_name = f'KEY_{char}'
                        press_report[2] = hid_keys.get(key_name, 0)
                    # === 数字 (0-9) ===
                    elif '0' <= char <= '9':
                        key_name = f'KEY_{char}'
                        press_report[2] = hid_keys.get(key_name, 0)
                    # === 記号 ===
                    else:
                        # 記号のマッピング: (Shift必要か, キー名)
                        symbol_map = {
                            '@': (True, 'KEY_2'),      # Shift+2 = @
                            '-': (False, 'KEY_MINUS'), # - キー
                            '.': (False, 'KEY_DOT'),   # . キー
                            '_': (True, 'KEY_MINUS'),  # Shift+- = _
                            '+': (True, 'KEY_EQUAL')   # Shift+= = +
                        }
                        if char in symbol_map:
                            shift, key_name = symbol_map[char]
                            press_report[2] = hid_keys.get(key_name, 0)
                        else:
                            # サポートされていない文字はスキップ
                            continue

                    # Shiftが必要な場合はモディファイアをセット
                    if shift:
                        press_report[0] = 0x02  # 左Shift

                    # キープレスとリリースを送信
                    fd.write(bytes(press_report))
                    await asyncio.sleep(0.02)
                    fd.write(bytes(release_report))
                    await asyncio.sleep(0.02)
                        
        except Exception as e:
            logging.error(f"Error typing email: {e}")

    def held1(self, btn):
        """
        ボタン1の長押しハンドラ
        
        単独長押し: リマップ機能のトグル
        ボタン2と同時長押し: メールアドレス入力
        ボタン3と同時長押し: シャットダウン
        """
        global REMAP_ENABLED
        self.button_states[1]["was_held"] = True
        
        if self.button_states[3]["was_held"]:
            # ボタン1+3長押し: シャットダウン
            logging.info("Btn 1+3 Held: Shutdown initiated.")
            self.button_states[1]["combination_detected"] = True
            self.button_states[3]["combination_detected"] = True
            asyncio.create_task(proxy_core.shutdown(self.loop))
        elif self.button_states[2]["was_held"]:
            # ボタン1+2長押し: メールアドレス入力
            logging.info("Btn 1+2 Held: Typing email.")
            self.button_states[1]["combination_detected"] = True
            self.button_states[2]["combination_detected"] = True
            asyncio.run_coroutine_threadsafe(self.send_email_address(), self.loop)
        else:
            # ボタン1単独長押し: リマップ切り替え
            REMAP_ENABLED = not REMAP_ENABLED
            state = "Enabled" if REMAP_ENABLED else "Disabled"
            logging.info(f"Btn 1 Held: Remap {state}")
            self.led.show_remap_state(REMAP_ENABLED)

    def released1(self, btn):
        """ボタン1のリリースハンドラ"""
        # 長押しでも組み合わせ検出でもない場合は短押しとして処理
        if not self.button_states[1]["was_held"] and not self.button_states[1]["combination_detected"]: 
            self.pressed1(btn)
        # 状態をリセット
        self.button_states[1]["was_held"] = False
        self.button_states[1]["combination_detected"] = False

    def pressed1(self, btn): 
        """ボタン1の短押しハンドラ: Alt+A を送信"""
        logging.info("Btn 1 Pressed: Alt+A")
        asyncio.run_coroutine_threadsafe(self.send_key_combination(0x04, 0x04), self.loop)

    def held2(self, btn):
        """ボタン2の長押しハンドラ"""
        self.button_states[2]["was_held"] = True
        if self.button_states[1]["was_held"]:
            # ボタン1+2長押し: メールアドレス入力
            logging.info("Btn 1+2 Held: Typing email.")
            self.button_states[1]["combination_detected"] = True
            self.button_states[2]["combination_detected"] = True
            asyncio.run_coroutine_threadsafe(self.send_email_address(), self.loop)

    def released2(self, btn):
        """ボタン2のリリースハンドラ"""
        if not self.button_states[2]["was_held"] and not self.button_states[2]["combination_detected"]: 
            self.pressed2(btn)
        self.button_states[2]["was_held"] = False
        self.button_states[2]["combination_detected"] = False

    def pressed2(self, btn): 
        """ボタン2の短押しハンドラ: Alt+Y を送信"""
        logging.info("Btn 2 Pressed: Alt+Y")
        asyncio.run_coroutine_threadsafe(self.send_key_combination(0x04, 0x1c), self.loop)

    def held3(self, btn):
        """ボタン3の長押しハンドラ"""
        self.button_states[3]["was_held"] = True
        if self.button_states[1]["was_held"]:
            # ボタン1+3長押し: シャットダウン
            logging.info("Btn 1+3 Held: Shutdown initiated.")
            self.button_states[1]["combination_detected"] = True
            self.button_states[3]["combination_detected"] = True
            asyncio.create_task(proxy_core.shutdown(self.loop))

    def released3(self, btn):
        """ボタン3のリリースハンドラ"""
        if not self.button_states[3]["was_held"] and not self.button_states[3]["combination_detected"]: 
            self.pressed3(btn)
        self.button_states[3]["was_held"] = False
        self.button_states[3]["combination_detected"] = False

    def pressed3(self, btn):
        """ボタン3の短押しハンドラ: スペースキーを送信"""
        logging.info("Btn 3 Pressed: Space")
        asyncio.run_coroutine_threadsafe(self.send_key_combination(0x00, 0x2c), self.loop)

async def device_monitor(loop):
    """
    デバイスモニタータスク
    
    定期的にシステムの入力デバイスをスキャンし、
    対象のキーボードデバイスを検出・管理します。
    
    Args:
        loop (asyncio.AbstractEventLoop): 非同期イベントループ
    """
    # 対象キーボードの名前パターン（正規表現）
    # HHKB Studio、HHKB Hybrid、PFU製キーボードにマッチ
    KEYBOARD_DEVICE_NAME_PATTERN = re.compile(r'HHKB-Studio[1-4] Keyboard|HHKB-Hybrid.*|PFU.*')
    
    # HID出力パスの設定を取得
    hid_paths = CONFIG.get("hid_paths", {})
    if "keyboard_outputs" in hid_paths:
        KEYBOARD_HID_OUTPUTS = hid_paths["keyboard_outputs"]
    elif "keyboard" in hid_paths:
        KEYBOARD_HID_OUTPUTS = [hid_paths["keyboard"]]
    else:
        KEYBOARD_HID_OUTPUTS = []
        logging.warning("No keyboard HID paths configured.")

    # 管理状態の初期化
    managed_keyboards = {}  # 管理中のキーボード: {パス: {task, hid_output}}
    available_keyboard_hids = set(KEYBOARD_HID_OUTPUTS)  # 利用可能なHID出力
    cached_device_paths = set()  # キャッシュされたデバイスパス
    cached_devices = {}  # キャッシュされたデバイスオブジェクト
    
    logging.info("Starting Keyboard Monitor...")

    while True:
        try:
            # 完了したタスクのクリーンアップ
            proxy_core.reap_dead_tasks(managed_keyboards, available_keyboard_hids, "Keyboard")
            
            # デバイスリストを取得
            current_device_paths = set(evdev.list_devices())
            
            # デバイスリストが変化した場合のみキャッシュを更新
            if current_device_paths != cached_device_paths:
                cached_devices = {}
                for path in current_device_paths:
                    try:
                        cached_devices[path] = evdev.InputDevice(path)
                    except (OSError, PermissionError):
                        # アクセス権限がないデバイスはスキップ
                        continue
                cached_device_paths = current_device_paths
            
            # 対象のキーボードデバイスをフィルタリング
            current_keyboards = {p: d for p, d in cached_devices.items() 
                               if KEYBOARD_DEVICE_NAME_PATTERN.match(d.name)}
            
            # デバイス接続管理
            proxy_core.manage_device_connections(
                current_keyboards, managed_keyboards, available_keyboard_hids, 
                KeyboardProxy, "Keyboard", loop
            )
            
        except Exception as e:
            logging.error(f"Monitor error: {e}", exc_info=True)
        
        # 5秒間隔でスキャン
        await asyncio.sleep(5)


# =============================================================================
# メインエントリーポイント
# =============================================================================
if __name__ == "__main__":
    # 非同期イベントループを取得
    loop = asyncio.get_event_loop()
    
    # グローバル例外ハンドラを設定
    loop.set_exception_handler(proxy_core.handle_exception)
    
    # シグナルハンドラを登録（SIGHUP, SIGTERM, SIGINT）
    # これらのシグナルを受信するとグレースフルシャットダウンを実行
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, lambda s=s: asyncio.create_task(proxy_core.shutdown(loop, s)))
    
    try:
        # GPIOボタンマネージャーを初期化
        keybow = KeyBowManager(loop)
        
        # デバイスモニタータスクを開始
        loop.create_task(device_monitor(loop))
        
        # イベントループを実行（無限ループ）
        loop.run_forever()
    finally:
        # クリーンアップ
        loop.close()
