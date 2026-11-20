#!/bin/bash
# =============================================================================
# Multi HID Proxy リリースビルドスクリプト
# =============================================================================
#
# このスクリプトは、Multi HID Proxyの配布用アーカイブを作成します。
#
# ビルド内容:
#   - Rust製マウスプロキシのクロスコンパイル（musl の静的バイナリ）
#   - 必要なファイルの収集
#   - 配布用tarアーカイブの作成
#
# 前提条件:
#   - Rust開発環境（cargo）
#   - zig と cargo-zigbuild（クロスコンパイルのリンカー）
#   - aarch64ターゲット（rustup target add aarch64-unknown-linux-musl）
#
# 使用方法:
#   ./build_release.sh              # デフォルト: aarch64 (Raspberry Pi)
#   ./build_release.sh --target native  # ホストアーキテクチャ用
#
# 出力:
#   multi-hid-proxy-release.tar.gz  # 配布用アーカイブ
#
# =============================================================================

# エラー発生時にスクリプトを終了
set -e

# =============================================================================
# パス設定
# =============================================================================
# スクリプトの場所を取得
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# プロジェクトルート（scriptsディレクトリの親）
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# 配布用ファイルを集めるディレクトリ
DIST_DIR="${PROJECT_ROOT}/dist"
# 出力するアーカイブ名
ARCHIVE_NAME="multi-hid-proxy-release.tar.gz"

# =============================================================================
# 引数の解析
# =============================================================================
# デフォルトターゲット: Raspberry Pi Zero 2W (aarch64)
TARGET_ARCH="aarch64"

while [[ $# -gt 0 ]]; do
    case $1 in
        --target)
            # ターゲットアーキテクチャを指定
            TARGET_ARCH="$2"
            shift 2
            ;;
        *)
            echo "不明なオプション: $1"
            exit 1
            ;;
    esac
done

echo "=== Multi HID Proxy ビルドスクリプト開始 ==="

# =============================================================================
# 1. Rustプロジェクトのビルド
# =============================================================================
echo "[1/4] Rustバイナリ (mouse_proxy_rs) をビルドしています..."

# Rustプロジェクトディレクトリに移動
cd "${PROJECT_ROOT}/rust/mouse_proxy_rs"

# cargoコマンドの存在確認
# ~/.cargo/env が存在する場合は読み込んでパスを通す
if ! command -v cargo &> /dev/null; then
    if [ -f "$HOME/.cargo/env" ]; then
        source "$HOME/.cargo/env"
    fi
fi

# cargoが見つからない場合はエラー
if ! command -v cargo &> /dev/null; then
    echo "エラー: cargo コマンドが見つかりません。Rust環境をインストールしてください。"
    exit 1
fi

# ビルドコマンドの初期設定
BUILD_CMD="cargo build --release"

# -----------------------------------------------------------------------------
# クロスコンパイル設定
# -----------------------------------------------------------------------------
if [ -n "$TARGET_ARCH" ] && [ "$TARGET_ARCH" != "native" ]; then
    echo "ターゲットアーキテクチャ: $TARGET_ARCH"
    
    if [[ "$TARGET_ARCH" == "aarch64" || "$TARGET_ARCH" == "aarch64-unknown-linux-musl" ]]; then
        # aarch64 (ARM 64-bit) ターゲット。musl で静的リンクし、Pi 側の glibc の版に依存しない
        TARGET_TRIPLE="aarch64-unknown-linux-musl"

        # クロスコンパイル用ツールの存在確認
        if ! command -v zig &> /dev/null; then
            echo "エラー: zig が見つかりません。"
            echo "docs/BUILD.md の「クロスコンパイル環境」に従って入れてください。"
            exit 1
        fi
        if ! cargo zigbuild --help &> /dev/null; then
            echo "エラー: cargo-zigbuild が見つかりません。"
            echo "docs/BUILD.md の「クロスコンパイル環境」に従って入れてください。"
            exit 1
        fi

        # Rustターゲットの確認
        if ! rustup target list --installed | grep -q "$TARGET_TRIPLE"; then
             echo "警告: Rustターゲット '$TARGET_TRIPLE' がインストールされていない可能性があります。"
             echo "実行してみてください: rustup target add $TARGET_TRIPLE"
             # 続行する（すでにあるかもしれないので）
        fi

        BUILD_CMD="cargo zigbuild --release --target $TARGET_TRIPLE"
        
        # クロスコンパイル時のバイナリパス
        BINARY_PATH="${PROJECT_ROOT}/rust/mouse_proxy_rs/target/$TARGET_TRIPLE/release/mouse_proxy_rs"
    else
        echo "エラー: サポートされていないアーキテクチャです: $TARGET_ARCH"
        exit 1
    fi
else
    # ネイティブビルド（ホストアーキテクチャ用）
    echo "ターゲットアーキテクチャ: native (host)"
    BINARY_PATH="${PROJECT_ROOT}/rust/mouse_proxy_rs/target/release/mouse_proxy_rs"
fi

# ビルド実行
echo "実行コマンド: $BUILD_CMD"
eval "$BUILD_CMD"

# ビルド結果の確認
if [ ! -f "$BINARY_PATH" ]; then
    echo "エラー: バイナリのビルドに失敗しました ($BINARY_PATH が見つかりません)"
    exit 1
fi

# =============================================================================
# 2. 配布用ディレクトリの準備
# =============================================================================
echo "[2/4] 配布用ディレクトリ ($DIST_DIR) を準備しています..."

# 既存のディレクトリを削除して再作成
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# =============================================================================
# 3. ファイルのコピー
# =============================================================================
echo "[3/4] 必要なファイルをコピーしています..."

# --- バイナリファイル ---
cp "$BINARY_PATH" "$DIST_DIR/"

# --- スクリプトと設定ファイル ---
cp "${PROJECT_ROOT}/scripts/install.sh" "$DIST_DIR/"
cp "${PROJECT_ROOT}/config/config.json.sample" "$DIST_DIR/"

# --- Pythonスクリプト ---
# キーボードプロキシはPythonで実装されているため含める
cp "${PROJECT_ROOT}/src/proxy_core.py" "$DIST_DIR/"
cp "${PROJECT_ROOT}/src/keyboard_proxy.py" "$DIST_DIR/"
cp "${PROJECT_ROOT}/src/hid_keys.py" "$DIST_DIR/"
cp "${PROJECT_ROOT}/scripts/setup_hid_gadget.sh" "$DIST_DIR/"

# --- systemdサービスファイル ---
cp "${PROJECT_ROOT}/systemd/keyboard-proxy.service" "$DIST_DIR/"
cp "${PROJECT_ROOT}/systemd/mouse-proxy@.service" "$DIST_DIR/"
cp "${PROJECT_ROOT}/systemd/multi-hid-gadget.service" "$DIST_DIR/"

# --- udevルール ---
cp "${PROJECT_ROOT}/udev/99-mouse-proxy.rules" "$DIST_DIR/"

# --- 実行権限の付与 ---
chmod +x "$DIST_DIR/install.sh"
chmod +x "$DIST_DIR/mouse_proxy_rs"
chmod +x "$DIST_DIR"/*.py
chmod +x "$DIST_DIR"/*.sh

# =============================================================================
# 4. アーカイブの作成
# =============================================================================
echo "[4/4] アーカイブ ($ARCHIVE_NAME) を作成しています..."

cd "${PROJECT_ROOT}"

# tarアーカイブを作成
# -c: 作成
# -z: gzip圧縮
# -v: 詳細出力
# -f: ファイル名指定
# -C: 指定ディレクトリをルートとしてアーカイブ
tar -czvf "$ARCHIVE_NAME" -C "$DIST_DIR" .

# =============================================================================
# 完了
# =============================================================================
echo "=== ビルド完了 ==="
echo "生成されたアーカイブ: ${PROJECT_ROOT}/${ARCHIVE_NAME}"
