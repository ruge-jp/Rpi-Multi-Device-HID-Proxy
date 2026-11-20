# 開発者ガイド

本ドキュメントはコードを読む・改造する・PR を出す開発者向けです。設計思想は [ARCHITECTURE.md](ARCHITECTURE.md)、ビルド手順は [BUILD.md](BUILD.md)、設定値は [CONFIGURATION.md](CONFIGURATION.md) を参照してください。

## 1. 開発環境セットアップ

### 1.1 前提
- Linux 環境（Pi 実機 or x86 ホスト）
- Python 3.9 以上
- Rust 安定版（stable）
- `git`, `jq`, `make` 相当のシェル環境

### 1.2 依存パッケージ

実機 Pi で動かす場合は [BUILD.md](BUILD.md#必要なツール) の依存リストをそのまま入れてください。

x86 ホストでクロスコンパイルする場合:

```bash
sudo apt install -y python3-evdev python3-gpiozero jq
rustup target add aarch64-unknown-linux-musl
```

加えて zig と cargo-zigbuild が必要です。入れ方は [BUILD.md](BUILD.md#クロスコンパイル環境オプション) を参照してください。

### 1.3 リポジトリのクローン

```bash
git clone <repo-url> Rpi-Multi-Device-HID-Proxy
cd Rpi-Multi-Device-HID-Proxy
```

新規開発は **作業ブランチ**を切ってから（直接 `main` を編集しないこと）:

```bash
git checkout -b feat/<short-description>
```

## 2. リポジトリ構成（コードレベル）

| パス | 役割 |
|---|---|
| `src/proxy_core.py` | 共有基盤。設定ロード（`load_config`）、ロギング設定、シグナル/シャットダウン、デバイス接続管理（`manage_device_connections`、`reap_dead_tasks`） |
| `src/keyboard_proxy.py` | キーボードプロキシ本体。`KeyboardProxy`（イベント処理＋HID 出力）、`KeyBowManager`（GPIO/LED）、`device_monitor`（5 秒ポーリング） |
| `src/hid_keys.py` | evdev のキー名（例: `KEY_A`）→ USB HID キーコード（例: `0x04`）の変換辞書 |
| `rust/mouse_proxy_rs/src/main.rs` | マウスプロキシ。tokio 非同期、`MouseState` で状態を保持し SYN 受信時に 7 バイト送信 |
| `rust/mouse_proxy_rs/Cargo.toml` | Rust 依存定義（`evdev`, `tokio`, `clap`, `anyhow`, `log`, `env_logger`） |
| `scripts/setup_hid_gadget.sh` | ConfigFS でガジェット構築。`config.json` から hidg 数を動的決定 |
| `scripts/install.sh` / `uninstall.sh` | ライフサイクル管理 |
| `scripts/build_release.sh` | aarch64 クロスコンパイル＋アーカイブ作成 |
| `systemd/*.service` | サービスユニット（multi-hid-gadget / keyboard-proxy / mouse-proxy@） |
| `udev/99-mouse-proxy.rules` | マウスデバイス検出ルール |
| `config/config.json.sample` | 設定例 |

## 3. ローカル実行とデバッグ

### 3.1 マウスプロキシ単体（実機 Pi）

```bash
cd rust/mouse_proxy_rs
cargo build --release
sudo ./target/release/mouse_proxy_rs /dev/input/eventX /dev/hidg1
```

ログを詳細にしたい場合:

```bash
RUST_LOG=debug sudo -E ./target/release/mouse_proxy_rs /dev/input/eventX /dev/hidg1
```

### 3.2 キーボードプロキシ単体（実機 Pi）

```bash
sudo python3 src/keyboard_proxy.py
```

`/etc/multi-hid-proxy/config.json` または `src/config.json`（同階層）または CWD の `config.json` のいずれかを使います（`src/proxy_core.py:84-89`）。開発中は `src/` に `config.json` を置くと最短で反映できます。

ログレベル変更は `config.json` の `logging.level` を `DEBUG` に。

### 3.3 PC 上での簡易動作確認

`/dev/hidg0` が存在しない開発 PC でロジックだけ確かめたい場合は、書き出し先を一時ファイルに変える小さなパッチを当てるか、`hid_paths` を `/tmp/fake_hid_kb` にして手動で `mkfifo` を作る方法があります。バイト列の確認は:

```bash
mkfifo /tmp/fake_hid_kb
# 別ターミナルで読み取り
hexdump -C /tmp/fake_hid_kb
```

ただし HID Descriptor 由来の挙動 (Boot Protocol/Report Protocol) はホスト側にも依存するため、最終確認は実機推奨です。

### 3.4 ログ確認（実機）

```bash
sudo journalctl -u keyboard-proxy.service -f
sudo journalctl -u 'mouse-proxy@*.service' -f
sudo journalctl -u multi-hid-gadget.service
```

## 4. テスト戦略

### 4.1 現状

自動テストは未整備です。動作確認は実機での手動検証に依存しています。

### 4.2 推奨される追加優先順

低コスト・高効果な順:

1. **Rust `MouseState::to_report()` の純関数テスト**: 入力（ボタン状態 + XYWheel）と期待バイト列を表で並べるだけで書けます。`rust/mouse_proxy_rs/src/main.rs:122-148` が対象。`#[cfg(test)] mod tests { ... }` を `main.rs` に追加するのが最小差分。
2. **Python `KeyboardProxy.remap()` のテーブルテスト**: 引数（`keycode`、`REMAP_ENABLED`、modifier）と期待出力（HID コード、Shift 抑制/付与フラグ）を表化。`src/keyboard_proxy.py:270-324` が対象。
3. **`hid_keys.py` の網羅性テスト**: 既知のキー名一覧と整合しているか、重複がないか。
4. **HID Descriptor の構文チェック**: `usbhid-dump` / `hid-tools` で `setup_hid_gadget.sh` 生成のディスクリプタを検証。

### 4.3 統合検証手順

実機で次の流れを通します:

```bash
# 1. 入力イベントを観察
sudo evtest /dev/input/eventX

# 2. HID 出力を観察（別ターミナル）
sudo hexdump -C /dev/hidg0

# 3. ホスト PC 側で USB HID Tester を開いて入力反映を確認
```

## 5. キーリマップの拡張方法

リマップロジックの本体は `src/keyboard_proxy.py:270-324` の `KeyboardProxy.remap()` です。

### 5.1 単純な置換（Shift 状態に依存しない）

`if keycode == 'KEY_X': keycode = 'KEY_Y'` を `# === 基本的なキーリマップ ===` ブロック（L296 付近）に追加します。

### 5.2 Shift 押下時のみ置換

`elif self.modifier & self.shift_bit:` のブロック（L304 付近）に追加。例: Shift+`@` を別の出力にしたい場合。

### 5.3 一時的に Shift を付ける/外す

実装は `self.is_shift_up` / `self.is_shift_down` というフラグで、`update_state()` がレポート組み立て時に modifier を一時的に書き換えます。

| フラグ | 意味 |
|---|---|
| `is_shift_up = True` | このキーを送るときだけ Shift を**付与**する |
| `is_shift_down = True` | このキーを送るときだけ Shift を**抑制**する |

例: 物理 `'`（KEY_APOSTROPHE）が単独で押されたとき `Shift+7`（&）として出力したい → `keycode = 'KEY_7'; self.is_shift_up = True`（既存の L319）。

### 5.4 未定義キーを使う場合

`hid_keys.py` に該当のキー名 → HID コードを追加してください。USB HID Usage Tables (Section 10) が一次情報です。

## 6. udev ルール / 対応デバイスの追加

### 6.1 対象デバイス名の確認

```bash
udevadm info /dev/input/eventX | grep ATTRS{name}
# または
cat /proc/bus/input/devices
```

### 6.2 ルール追加

`udev/99-mouse-proxy.rules` に以下を追記:

```
ACTION=="add", SUBSYSTEM=="input", KERNEL=="event*", \
  ATTRS{name}=="<デバイス名のパターン>", \
  TAG+="systemd", ENV{SYSTEMD_WANTS}+="mouse-proxy@%k.service"
```

`%k` はカーネルが付けた名前（`event2` 等）に展開され、`mouse-proxy@event2.service` として `mouse-proxy@.service` テンプレートが起動します。テンプレート側では `%I` で `event2` が展開され、`/dev/input/event2` が引数として渡されます（`systemd/mouse-proxy@.service:8`）。

### 6.3 反映

```bash
sudo cp udev/99-mouse-proxy.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### 6.4 キーボード側のデバイス追加

キーボードは udev ではなく `device_monitor`（`src/keyboard_proxy.py:710-775`）が正規表現で検出します。対象パターンの追加は同関数内の正規表現を編集してください。

## 7. HID レポート記述子のカスタマイズ

`scripts/setup_hid_gadget.sh:168` (キーボード) / `:199` (マウス) のバイト列が HID Report Descriptor です。変更する場合は **必ず**:

- バイト列の構文を USB HID Tool（hidrd-convert など）で検証
- `protocol` (`echo 1/2 > protocol`) と `subclass` を新仕様に合わせる
- `report_length` を変更後のレポートサイズに更新
- 送信側コード（`src/keyboard_proxy.py` の `update_state` または `rust/mouse_proxy_rs/src/main.rs` の `to_report`）も新レイアウトに揃える

3 か所のうち 1 か所でもズレるとホスト側で「Code 10」エラーになるか、入力が認識されなくなります。コミットは記述子・コード・report_length の 3 点を 1 つの diff に含めてください。

## 8. リリース手順

### 8.1 アーカイブ作成

```bash
./scripts/build_release.sh
# 生成物: multi-hid-proxy-release.tar.gz
```

ネイティブ（実機 Pi 上で実機向け）ビルドの場合:

```bash
./scripts/build_release.sh --target native
```

### 8.2 含まれるファイル

`scripts/build_release.sh:154-195` を参照。Python ソース、Rust バイナリ、systemd ユニット、udev ルール、`config.json.sample`、インストールスクリプトが含まれます。

### 8.3 バージョニング

現状バージョニング規約は未定義です。今後 SemVer (`vX.Y.Z`) を提案します:
- Major: HID Descriptor の互換性を破る変更、設定ファイルスキーマ非互換
- Minor: 新機能（新ボタン動作、対応デバイス追加等）
- Patch: バグ修正、ドキュメント

タグは `git tag vX.Y.Z` で打ち、リリース時に `multi-hid-proxy-release.tar.gz` を GitHub Release に添付してください。

## 9. コーディング規約・PR ガイドライン

### 9.1 言語

- **コミットメッセージ・コメント・ログ**: 日本語ベース。技術用語（`asyncio`, `evdev` 等）は英語のまま。
- **コード（識別子）**: 英語。
- **エラーメッセージ**: 日本語可（既存に合わせる）。

### 9.2 コード品質

- Python: PEP 8 を緩く守る。`black` / `ruff` の導入は今後の課題。
- Rust: `cargo fmt` をコミット前にかける。`cargo clippy --release` で警告ゼロを目指す。

### 9.3 PR チェックリスト

- [ ] 作業ブランチで commit している（`main` 直接コミットしていない）
- [ ] 動作確認（実機 or 単体テスト）の手順とログを PR 説明に書いている
- [ ] HID Descriptor を変えた場合、送信側コードと `report_length` も同 PR に含める
- [ ] 設定ファイルのスキーマを変えた場合、`config.json.sample` と `CONFIGURATION.md` を更新
- [ ] GPIO ピン・LED 配線・ボタン動作を変えた場合、`HARDWARE.md` も更新
- [ ] 機能変更時は `README.md` の概要も必要なら更新

### 9.4 LED 実装の歴史的経緯（重要）

本プロジェクトの LED 実装は途中で **誤った方向に分岐していた時期**があります。元々のターゲットは **Pimoroni Keybow Mini** (APA102 / SPI 駆動) で、コード上もクラス名は `KeyBowManager` のまま残っていますが、git commit `cfc1ee0` (2026-01-29) で `rpi_ws281x` (WS2812B 専用) が誤って導入され、ドキュメント・サンプル設定・install スクリプト全体が WS2812B 前提で書かれた期間がありました。実機 (Keybow Mini) では当然光らず、長期間 LED が機能していませんでした。

2026-05-04 の修正で **APA102 / `python3-spidev` ベースに作り直し**、`/boot/firmware/config.txt` の `dtparam=spi=on` 自動追記も追加しました。LED 周りを編集する際は:

- `LedStatusManager` クラス (`src/keyboard_proxy.py`) は **APA102 プロトコルを spidev で直叩き**する実装。WS2812B 用ライブラリに戻さない。
- 設定キーは `gpio_pin` ではなく `spi_bus` / `spi_device` / `spi_hz`。
- 「LED が光らない」と言われたら、**まず SPI が有効か** (`ls /dev/spidev0.*`) を確認。

別の WS2812B ストリップを使いたい場合は `LedStatusManager` を別実装に差し替えてください。

### 9.5 Critical Protocols（CLAUDE.md より抜粋）

リポジトリの最上位（または `~/CLAUDE.md`）に AI コーディングエージェント向けのルールがあります。人間の開発者にも有用な要点:

- 既存テストを失敗させた場合、まずは実装側のバグを疑う（テスト改変は最終手段）
- ドキュメント同期: 機能変更時は README/INSTALL/BUILD/CONFIGURATION/HARDWARE/ARCHITECTURE/DEVELOPMENT/AGENTS のうち関連するものを同 PR で更新
- 機密情報をコミットしない（`.env`, トークン等）
