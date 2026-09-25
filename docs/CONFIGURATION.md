# 設定ガイド

## 設定ファイル

設定ファイルは `/etc/multi-hid-proxy/config.json` にあります。

### 設定例

```json
{
  "email_address": "your.email@domain.com",
  "gpio_settings": {
    "hold_time": 1.5,
    "bounce_time": 0.05,
    "combination_check_delay": 0.2
  },
  "led_settings": {
    "enabled": true,
    "led_count": 3,
    "spi_bus": 0,
    "spi_device": 0,
    "spi_hz": 4000000,
    "brightness": 1,
    "boot_self_test": true,
    "colors": {
      "remap_enabled": [0, 255, 0],
      "remap_disabled": [255, 0, 0]
    }
  },
  "logging": {
    "level": "INFO"
  },
  "hid_paths": {
    "keyboard_outputs": ["/dev/hidg0"],
    "mouse_outputs": ["/dev/hidg1", "/dev/hidg2"]
  }
}
```

## 設定項目の詳細

### email_address

GPIOボタンのマクロ機能で使用するメールアドレス。ボタン長押しでこのアドレスを入力できます。

### gpio_settings

GPIOボタンの動作設定。

| パラメータ | 説明 | デフォルト |
|-----------|------|-----------|
| `hold_time` | 長押し判定時間（秒） | 1.5 |
| `bounce_time` | チャタリング防止時間（秒） | 0.05 |
| `combination_check_delay` | コンビネーション判定遅延（秒） | 0.2 |

### led_settings

Pimoroni Keybow Mini の APA102 LED の設定（SPI 駆動）。

| パラメータ | 説明 | デフォルト |
|-----------|------|-----------|
| `enabled` | LED機能の有効/無効 | true |
| `led_count` | LED数 | 3 |
| `spi_bus` | 利用する SPI バス番号（`/dev/spidevX.Y` の X） | 0 |
| `spi_device` | 利用する SPI デバイス番号（`/dev/spidevX.Y` の Y） | 0 |
| `spi_hz` | SPI クロック (Hz)。APA102 は 1〜8MHz が安定 | 4000000 |
| `brightness` | 明るさ（0-255）。APA102 のグローバル輝度 5bit にマップ。`0` を指定すると完全消灯 (0/31)。`1`〜`7` でも丸めで `0/31` にならないよう最低 `1/31` を保証 | 1 |
| `boot_self_test` | 起動時に赤→緑→青のセルフテストを流すか。ハード結線確認に有用 | true |
| `colors.remap_enabled` | リマップ有効時の色 [R,G,B] | [0,255,0] (緑) |
| `colors.remap_disabled` | リマップ無効時の色 [R,G,B] | [255,0,0] (赤) |

SPI が無効の場合は LED が動きません。`/boot/firmware/config.txt` に `dtparam=spi=on` が必要です（`scripts/install.sh` で自動追記）。LED が点灯しないときの切り分けは [HARDWARE.md §6.2.1](HARDWARE.md#621-led-が点灯しないときのチェック順序) を参照してください。

### logging

ログ設定。

| パラメータ | 説明 | 選択肢 |
|-----------|------|--------|
| `level` | ログレベル | DEBUG, INFO, WARNING, ERROR |

### hid_paths

HIDデバイスパスの設定。

| パラメータ | 説明 |
|-----------|------|
| `keyboard_outputs` | キーボード出力デバイスのリスト |
| `mouse_outputs` | マウス出力デバイスのリスト |

デバイス数を増減する場合は、このリストを編集してください。変更後は再起動が必要です。

## udevルールのカスタマイズ

新しいマウスデバイスを追加するには、`/etc/udev/rules.d/99-mouse-proxy.rules` を編集します。

### 現在のルール

```
# HHKB Studio Mouse
ACTION=="add", SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="HHKB-Studio[1-4] Mouse", TAG+="systemd", ENV{SYSTEMD_WANTS}+="mouse-proxy@%k.service"

# Logitech Mouse
ACTION=="add", SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="Logitech*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="mouse-proxy@%k.service"
```

### 新しいデバイスの追加

1. デバイス名を確認

```bash
cat /proc/bus/input/devices
```

または

```bash
udevadm info /dev/input/eventX
```

2. ルールを追加

```
# Example: Razer Mouse
ACTION=="add", SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="Razer*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="mouse-proxy@%k.service"
```

3. ルールを再読み込み

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## GPIOボタンの配線

GPIOボタンの物理配線・ピンマップ・短押し/長押し/組み合わせ動作の一覧は [HARDWARE.md](HARDWARE.md#3-gpio-ボタン配線) を参照してください。

要点のみ抜粋:

| ボタン | GPIO | 短押し | 長押し |
|-------|------|--------|--------|
| ボタン1 | GPIO 6 | Alt+A | リマップ機能の有効/無効トグル |
| ボタン2 | GPIO 22 | Alt+Y | （単独では未割当） |
| ボタン3 | GPIO 17 | Space | （単独では未割当） |
| ボタン1+2（同時長押し） | — | — | メールアドレス入力 |
| ボタン1+3（同時長押し） | — | — | システムシャットダウン |

※ GPIO 番号はソースコード（`src/keyboard_proxy.py:446-458`）で定義されています。変更にはソース編集が必要です。

## キーリマップの設定

キーリマップは `src/keyboard_proxy.py` の `KeyboardProxy.remap()` メソッドで定義されています。

現在のリマップ例:
- CapsLock → Left Control

カスタマイズする場合は、ソースコードを直接編集してください。

## サービスの管理

### サービスの再起動

設定変更後:

```bash
sudo systemctl restart keyboard-proxy.service
```

### ログの確認

```bash
# キーボードプロキシ
sudo journalctl -u keyboard-proxy.service -f

# マウスプロキシ
sudo journalctl -u mouse-proxy@event*.service -f

# HIDガジェット
sudo journalctl -u multi-hid-gadget.service
```

### デバッグモード

詳細なログを出力するには、`config.json` のログレベルを変更:

```json
{
  "logging": {
    "level": "DEBUG"
  }
}
```
