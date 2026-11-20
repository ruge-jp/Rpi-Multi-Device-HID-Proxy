# ビルドガイド

このドキュメントでは、ソースコードからプロジェクトをビルドする方法を説明します。

## 必要なツール

### Rust環境

マウスプロキシ（`mouse_proxy_rs`）のビルドにはRust環境が必要です。

```bash
# Rustのインストール
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env
```

### クロスコンパイル環境（オプション）

開発マシンからRaspberry Pi用にビルドする場合:

```bash
# aarch64ターゲットの追加
rustup target add aarch64-unknown-linux-musl

# zig と cargo-zigbuild のインストール（x86_64 ホスト。~/.local/bin が PATH にある前提）
ZIG_VERSION=0.17.1
ZIGBUILD_VERSION=v0.24.1
mkdir -p ~/.local/opt ~/.local/bin
curl -fsSL "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" | tar -xJ -C ~/.local/opt
ln -sf ~/.local/opt/zig-x86_64-linux-${ZIG_VERSION}/zig ~/.local/bin/zig
curl -fsSL "https://github.com/rust-cross/cargo-zigbuild/releases/download/${ZIGBUILD_VERSION}/cargo-zigbuild-x86_64-unknown-linux-musl.tar.xz" | tar -xJ -C ~/.local/bin --strip-components=1 --wildcards '*/cargo-zigbuild'
```

aarch64 向けのバイナリは musl の静的リンクなので、Pi 側の glibc の版に依存しません。

### Python依存関係

```bash
sudo apt install python3-evdev python3-gpiozero
```

オプション（Keybow Mini の APA102 LED を使用する場合）:

```bash
sudo apt install python3-spidev
# /boot/firmware/config.txt に dtparam=spi=on を追記して再起動も必要
```

## ビルド方法

### 自動ビルド（推奨）

`build_release.sh` スクリプトを使用してリリースアーカイブを作成:

```bash
# Raspberry Pi Zero 2W (aarch64) 向けにビルド
./scripts/build_release.sh

# ネイティブビルド（開発マシン上でテスト用）
./scripts/build_release.sh --target native
```

ビルド完了後、`multi-hid-proxy-release.tar.gz` が生成されます。

### 手動ビルド

#### 1. マウスプロキシのビルド

```bash
cd rust/mouse_proxy_rs

# ネイティブビルド
cargo build --release

# クロスコンパイル（aarch64）
cargo zigbuild --release --target aarch64-unknown-linux-musl
```

バイナリは以下に生成されます:

- ネイティブ: `target/release/mouse_proxy_rs`
- クロスコンパイル: `target/aarch64-unknown-linux-musl/release/mouse_proxy_rs`

#### 2. Pythonスクリプト

Pythonスクリプトはビルド不要です。そのまま使用できます。

## リリースアーカイブの内容

`build_release.sh` で生成されるアーカイブには以下が含まれます:

```
multi-hid-proxy-release.tar.gz
├── install.sh
├── config.json.sample
├── mouse_proxy_rs
├── proxy_core.py
├── keyboard_proxy.py
├── hid_keys.py
├── setup_hid_gadget.sh
├── keyboard-proxy.service
├── mouse-proxy@.service
├── multi-hid-gadget.service
└── 99-mouse-proxy.rules
```

## 開発環境での実行

### マウスプロキシの単体テスト

```bash
cd rust/mouse_proxy_rs
cargo run -- /dev/input/eventX /dev/hidgY
```

### キーボードプロキシの単体テスト

```bash
sudo python3 src/keyboard_proxy.py
```

## トラブルシューティング

### クロスコンパイルエラー

`zig` または `cargo zigbuild` が見つからない場合は、[クロスコンパイル環境](#クロスコンパイル環境オプション) の手順で入れ、`zig version` と `cargo zigbuild --version` が通ることを確認してください。

### Rustターゲットがない

```bash
rustup target add aarch64-unknown-linux-musl
```

### Python依存関係エラー

```bash
# evdevのインストール
sudo apt install python3-evdev

# gpiozeroのインストール
sudo apt install python3-gpiozero
```
