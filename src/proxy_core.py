#!/usr/bin/python3
"""
Proxy Core Module - プロキシコアモジュール
===========================================

このモジュールは、キーボードプロキシとマウスプロキシで共有される
共通ロジックを提供します。

主な機能:
- 設定ファイルの読み込みとマージ
- ロギングの設定
- グレースフルシャットダウン処理
- デバイス接続管理
- タスクのライフサイクル管理

使用方法:
    import proxy_core
    config = proxy_core.load_config()
    proxy_core.setup_logging(config)
"""

import logging
import asyncio
import copy
import json
import os
import signal

# =============================================================================
# デフォルト設定
# =============================================================================
# 設定ファイルが見つからない場合や、設定ファイルに項目が欠けている場合に
# 使用されるフォールバック値を定義します。
DEFAULT_CONFIG = {
    # メールアドレス入力機能で使用するメールアドレス
    "email_address": "test@example.com",
    
    # GPIOボタンの設定
    "gpio_settings": {
        "hold_time": 1.5,              # 長押し判定時間（秒）
        "bounce_time": 0.05,           # チャタリング防止時間（秒）
        "combination_check_delay": 0.2  # ボタン組み合わせ検出の遅延（秒）
    },
    
    # ロギング設定
    "logging": {
        "level": "ERROR"  # ログレベル: DEBUG, INFO, WARNING, ERROR, CRITICAL
    },
    
    # HIDデバイスのパス設定
    "hid_paths": {
        "keyboard": "/dev/hidg0",                    # キーボード出力パス
        "mouse_outputs": ["/dev/hidg1", "/dev/hidg2"]  # マウス出力パス（複数対応）
    },

    # APA102 LED (Pimoroni Keybow Mini) の設定
    # リマップ機能 (US→JIS) の有効/無効状態を視覚通知する
    # SPI 経由 (GPIO10/MOSI = データ、GPIO11/SCLK = クロック)
    "led_settings": {
        "enabled": True,            # LED 機能を使うか
        "led_count": 3,             # LED 個数 (Keybow Mini は 3)
        "spi_bus": 0,               # /dev/spidev{spi_bus}.{spi_device}
        "spi_device": 0,            # Keybow Mini は /dev/spidev0.0
        "spi_hz": 4000000,          # SPI クロック (Hz)。APA102 は 4-8MHz が安定
        "brightness": 1,            # 明るさ (0-255)、APA102 グローバル輝度 5bit にマップ
                                    # 1 は設定範囲で一番暗い点灯 (0/31 ではなく最低 1/31 を保証)
        "boot_self_test": True,     # 起動時に赤→緑→青の動作確認シーケンスを流す
        "colors": {
            "remap_enabled": [0, 255, 0],   # リマップ有効: 緑
            "remap_disabled": [255, 0, 0]   # リマップ無効: 赤
        }
    }
}


def _deep_merge(base, override):
    """
    base 辞書に override 辞書を再帰的にマージします（破壊的操作）。

    base にも override にも同名キーが辞書として存在する場合は再帰、
    それ以外の場合は override の値で上書きします。

    旧実装は DEFAULT_CONFIG のキーしか取り込まなかったため、
    ユーザー設定にだけ存在するキー (例: led_settings) が黙って捨てられていました。
    ここでは override 主導でループし、ユーザー設定の値を確実に反映します。
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(config_path=None):
    """
    設定ファイルを読み込み、デフォルト設定とマージして返します。
    
    設定ファイルの検索順序:
    1. 引数で指定されたパス（指定された場合）
    2. /etc/multi-hid-proxy/config.json（システムワイドな設定）
    3. スクリプトと同じディレクトリの config.json
    4. カレントディレクトリの config.json
    
    Args:
        config_path (str, optional): 設定ファイルのパス。
                                     Noneの場合は自動検索。
    
    Returns:
        dict: 読み込まれた設定とデフォルト設定をマージした辞書。
              設定ファイルが見つからない場合はデフォルト設定を返す。
    
    Note:
        設定のマージは深い再帰マージで、
        ネストされた辞書同士は再帰的にマージされ、
        DEFAULT_CONFIG に無いキーもユーザー設定からそのまま取り込まれます。
    """
    # 引数でパスが指定されていない場合、複数の場所を検索
    if config_path is None:
        # スクリプトのディレクトリを取得
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # 設定ファイルの検索パス（優先度順）
        search_paths = [
            "/etc/multi-hid-proxy/config.json",  # システムワイド設定
            os.path.join(script_dir, "config.json"),  # スクリプト横
            "config.json"  # カレントディレクトリ
        ]
        
        # 各パスを順番にチェックし、最初に見つかったものを使用
        for path in search_paths:
            if os.path.exists(path):
                config_path = path
                print(f"[Proxy-Core-DEBUG] Loading config from: {config_path}")
                break
        
        # どのパスにも見つからなかった場合の警告
        if config_path is None:
            logging.warning("Config file not found. Searched: " + ", ".join(search_paths))
            config_path = search_paths[0]  # デフォルトパスを設定（後でエラー処理）
    
    # デフォルト設定のディープコピーを作成。
    # 浅いコピー (.copy()) では DEFAULT_CONFIG 内のネスト辞書 (gpio_settings,
    # led_settings, hid_paths) が同じ参照のままで、_deep_merge が破壊的に
    # 更新するため DEFAULT_CONFIG 自体がユーザー設定で汚染されてしまう。
    # 必ず copy.deepcopy を使うこと。
    config = copy.deepcopy(DEFAULT_CONFIG)
    
    try:
        if os.path.exists(config_path):
            # UTF-8エンコーディングで設定ファイルを読み込み
            with open(config_path, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
                print(f"[Proxy-Core] Loaded config: {config_path}")

                # 再帰的なマージ処理。
                # ユーザー設定にだけ存在するキーも保持される。
                _deep_merge(config, user_config)

                return config
        else:
            logging.warning(f"Config file {config_path} not found. Using defaults.")
            return config
            
    except Exception as e:
        # JSONパースエラーなどが発生した場合
        logging.error(f"Error loading config: {e}. Using defaults.")
        return config


def setup_logging(config):
    """
    設定に基づいてロギングを初期化します。
    
    systemdサービスとして実行される場合と、ターミナルから直接実行される場合で
    ログフォーマットを自動的に切り替えます。
    
    Args:
        config (dict): load_config()で取得した設定辞書。
                       logging.level キーでログレベルを指定。
    
    Note:
        INVOCATION_ID環境変数はsystemdによって設定されるため、
        この変数の有無でsystemd経由の起動かどうかを判定できます。
        systemd経由の場合は、タイムスタンプはjournaldが付与するため省略します。
    """
    # 設定からログレベルを取得（デフォルトはERROR）
    log_level_str = config.get("logging", {}).get("level", "ERROR")
    
    # 文字列をloggingモジュールの定数に変換
    # 無効な値が指定された場合はERRORにフォールバック
    log_level = getattr(logging, log_level_str.upper(), logging.ERROR)
    
    # systemd経由の起動かどうかで出力フォーマットを変更
    if os.getenv('INVOCATION_ID'):
        # systemdサービスとして実行中 - タイムスタンプなし（journaldが付与）
        logging.basicConfig(level=log_level, format='[%(name)s|%(levelname)s] %(message)s')
    else:
        # 直接実行 - タイムスタンプ付き
        logging.basicConfig(level=log_level, format='[%(asctime)s|%(name)s|%(levelname)s] %(message)s')
        
    logging.info(f"Logger initialized. Level: {log_level_str}")


async def shutdown(loop, signal=None):
    """
    グレースフルシャットダウンを実行します。
    
    実行中のすべての非同期タスクをキャンセルし、
    イベントループを停止します。
    
    Args:
        loop (asyncio.AbstractEventLoop): 停止するイベントループ。
        signal (signal.Signals, optional): シャットダウンをトリガーしたシグナル。
                                          Noneの場合はプログラム内部からの呼び出し。
    
    Note:
        この関数は、SIGTERM、SIGINT、SIGHUPなどのシグナルハンドラから
        呼び出されることを想定しています。
    """
    # シグナルによるシャットダウンの場合はログに記録
    if signal: 
        logging.info(f"Received exit signal {signal.name}...")
    
    # 現在のタスク以外のすべての実行中タスクを取得
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    
    # 各タスクにキャンセルを要求
    for task in tasks: 
        task.cancel()
    
    logging.info(f"Cancelling {len(tasks)} tasks.")
    
    # すべてのタスクが終了するまで待機
    # return_exceptions=Trueにより、CancelledErrorを例外として発生させない
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # イベントループを停止
    loop.stop()
    logging.info("Service shutdown complete.")


def handle_exception(loop, context):
    """
    グローバル例外ハンドラ。
    
    asyncioイベントループで捕捉されなかった例外を処理し、
    アプリケーションをシャットダウンします。
    
    Args:
        loop (asyncio.AbstractEventLoop): 例外が発生したイベントループ。
        context (dict): 例外に関する情報を含む辞書。
                       'exception': 発生した例外オブジェクト（存在する場合）
                       'message': エラーメッセージ
    
    Note:
        このハンドラはloop.set_exception_handler()で登録して使用します。
    """
    # 例外オブジェクトがある場合はそれを、なければメッセージを取得
    msg = context.get("exception", context["message"])
    logging.error(f"Unhandled exception: {msg}", exc_info=context.get('exception'))
    
    # 致命的なエラーとしてシャットダウンを開始
    asyncio.create_task(shutdown(loop=loop))


def reap_dead_tasks(managed_devices, available_hids, device_type_name):
    """
    完了したタスクをクリーンアップし、HID出力リソースを解放します。
    
    定期的に呼び出されることで、切断されたデバイスや
    エラーで終了したプロキシタスクを検出・処理します。
    
    Args:
        managed_devices (dict): 管理中のデバイス情報。
                               キー: デバイスパス
                               値: {'task': asyncio.Task, 'hid_output': str}
        available_hids (set): 利用可能なHID出力パスのセット。
                             解放されたパスはここに追加される。
        device_type_name (str): ログ出力用のデバイスタイプ名（例: "Keyboard", "Mouse"）。
    
    Note:
        この関数はmanaged_devicesとavailable_hidsを直接変更します。
    """
    # 完了したタスクのデバイスパスを収集
    dead_tasks_paths = [path for path, info in managed_devices.items() if info['task'].done()]
    
    for path in dead_tasks_paths:
        logging.info(f"Cleaning up finished {device_type_name} task: {path}")
        
        # 管理リストから削除し、情報を取得
        info = managed_devices.pop(path)
        
        # タスクが例外で終了した場合はエラーログを出力
        if info['task'].exception():
            logging.error(f"{device_type_name} task {path} ended with exception: {info['task'].exception()}")
        
        # HID出力パスを利用可能なプールに戻す
        available_hids.add(info['hid_output'])


def manage_device_connections(current_devices, managed_devices, available_hids, proxy_class, device_type_name, loop):
    """
    デバイスの接続・切断を検出し、プロキシタスクを管理します。
    
    新しく接続されたデバイスに対してはプロキシタスクを起動し、
    切断されたデバイスのタスクはキャンセルします。
    
    Args:
        current_devices (dict): 現在検出されているデバイス。
                               キー: デバイスパス, 値: デバイスオブジェクト
        managed_devices (dict): 現在管理中のデバイス情報。
                               キー: デバイスパス
                               値: {'task': asyncio.Task, 'hid_output': str}
        available_hids (set): 利用可能なHID出力パスのセット。
        proxy_class: プロキシクラス（KeyboardProxyまたはMouseProxy）。
                    input_device_path, hid_output_path, loopを引数に取るコンストラクタが必要。
        device_type_name (str): ログ出力用のデバイスタイプ名。
        loop (asyncio.AbstractEventLoop): タスクを実行するイベントループ。
    
    Note:
        この関数はmanaged_devices、available_hidsを直接変更します。
        利用可能なHID出力がない場合、新しいデバイスは接続されません。
    """
    # 現在のデバイスパスと管理中のデバイスパスをセットに変換
    current_paths = set(current_devices.keys())
    managed_paths = set(managed_devices.keys())
    
    # === 新しく接続されたデバイスの処理 ===
    for path in (current_paths - managed_paths):
        # 利用可能なHID出力がない場合はスキップ
        if not available_hids:
            logging.warning(f"New {device_type_name} {path} found, but no available HID outputs.")
            continue
        
        # HID出力パスを割り当て（セットからポップ）
        output_path = available_hids.pop()
        device = current_devices[path]
        
        logging.info(f"Detected new {device_type_name}: {path} ({device.name}) -> {output_path}")
        
        # プロキシインスタンスを作成し、タスクとして起動
        proxy = proxy_class(input_device_path=path, hid_output_path=output_path, loop=loop)
        task = asyncio.create_task(proxy.run())
        
        # 管理リストに追加
        managed_devices[path] = {'task': task, 'hid_output': output_path}
    
    # === 切断されたデバイスの処理 ===
    for path in (managed_paths - current_paths):
        logging.info(f"{device_type_name} {path} disconnected. Cleaning up.")
        
        # 管理リストから削除
        info = managed_devices.pop(path)
        
        # 実行中のタスクをキャンセル
        info['task'].cancel()
        
        # HID出力パスを利用可能なプールに戻す
        available_hids.add(info['hid_output'])
        logging.info(f"Task cancelled, HID output {info['hid_output']} freed.")
