//! Mouse Proxy (Rust) - マウスプロキシ
//!
//! このモジュールは、USBマウスデバイスからの入力を受け取り、
//! USB HIDガジェットデバイスに転送する高性能なプロキシを提供します。
//!
//! # 特徴
//! - 非同期I/O（tokio）による高効率な処理
//! - 低レイテンシーのイベント転送
//! - 5ボタンマウス対応（左、右、中、サイド、エクストラ）
//! - 16ビット精度の移動量とスクロール
//!
//! # 使用方法
//! ```bash
//! mouse_proxy_rs /dev/input/eventX /dev/hidgY
//! ```
//!
//! # HIDレポート形式
//! 7バイトのレポートを使用:
//! - byte 0: ボタン状態（ビットマスク）
//! - bytes 1-2: X軸移動量（16ビット符号付き整数、リトルエンディアン）
//! - bytes 3-4: Y軸移動量（16ビット符号付き整数、リトルエンディアン）
//! - bytes 5-6: ホイールスクロール量（16ビット符号付き整数、リトルエンディアン）

use anyhow::{Context, Result};
use clap::Parser;
use evdev::{Device, InputEventKind, Key, RelativeAxisType};
use log::{error, info};
use std::fs::OpenOptions;
use std::io::Write;

// =============================================================================
// コマンドライン引数の定義
// =============================================================================
/// マウスプロキシのコマンドライン引数構造体
///
/// clapクレートを使用して引数をパースします。
#[derive(Parser, Debug)]
#[command(author, version, about, long_about = None)]
struct Args {
    /// 入力デバイスのパス（例: /dev/input/event0）
    /// マウスイベントを読み取るソースデバイス
    #[arg(required = true)]
    input_device: String,
    
    /// 出力デバイスのパス（例: /dev/hidg1）
    /// HIDレポートを書き込むガジェットデバイス
    #[arg(required = true)]
    output_device: String,
}

// =============================================================================
// 定数定義
// =============================================================================
/// HIDレポートのサイズ（バイト）
/// 
/// レポート構造:
/// - 1バイト: ボタン状態
/// - 2バイト: X軸移動量
/// - 2バイト: Y軸移動量
/// - 2バイト: ホイールスクロール量
const REPORT_SIZE: usize = 7;

// =============================================================================
// マウス状態構造体
// =============================================================================
/// マウスの現在状態を保持する構造体
///
/// 入力イベントから状態を更新し、HIDレポートを生成するために使用します。
struct MouseState {
    /// 左ボタンの状態（押下時: 1, 離時: 0）
    btn_left: u8,
    /// 右ボタンの状態（押下時: 2, 離時: 0）
    btn_right: u8,
    /// 中ボタン（ホイールクリック）の状態（押下時: 4, 離時: 0）
    btn_middle: u8,
    /// サイドボタン（戻る）の状態（押下時: 8, 離時: 0）
    btn_side: u8,
    /// エクストラボタン（進む）の状態（押下時: 16, 離時: 0）
    btn_extra: u8,
    /// X軸の相対移動量（正: 右, 負: 左）
    move_x: i16,
    /// Y軸の相対移動量（正: 下, 負: 上）
    move_y: i16,
    /// ホイールスクロール量（正: 上, 負: 下）
    scroll_y: i16,
}

impl MouseState {
    /// 新しいMouseStateインスタンスを作成します。
    ///
    /// すべてのボタンは離された状態、移動量はゼロで初期化されます。
    fn new() -> Self {
        Self {
            btn_left: 0,
            btn_right: 0,
            btn_middle: 0,
            btn_side: 0,
            btn_extra: 0,
            move_x: 0,
            move_y: 0,
            scroll_y: 0,
        }
    }

    /// 現在の状態からHIDレポートを生成します。
    ///
    /// # 戻り値
    /// 7バイトのHIDレポート配列
    ///
    /// # レポート形式
    /// ```text
    /// byte 0: ボタンビットマスク
    ///         bit 0: 左ボタン
    ///         bit 1: 右ボタン
    ///         bit 2: 中ボタン
    ///         bit 3: サイドボタン
    ///         bit 4: エクストラボタン
    /// bytes 1-2: X移動量（リトルエンディアン16ビット符号付き整数）
    /// bytes 3-4: Y移動量（リトルエンディアン16ビット符号付き整数）
    /// bytes 5-6: ホイール量（リトルエンディアン16ビット符号付き整数）
    /// ```
    fn to_report(&self) -> [u8; REPORT_SIZE] {
        // すべてのボタン状態をOR演算で結合
        let buttons = self.btn_left | self.btn_right | self.btn_middle | self.btn_side | self.btn_extra;
        
        // レポート配列を初期化
        let mut report = [0u8; REPORT_SIZE];
        
        // ボタン状態をセット
        report[0] = buttons;
        
        // 移動量とスクロール量をリトルエンディアンバイト配列に変換
        let x = self.move_x.to_le_bytes();
        let y = self.move_y.to_le_bytes();
        let w = self.scroll_y.to_le_bytes();
        
        // X軸移動量（2バイト）
        report[1] = x[0]; 
        report[2] = x[1];
        // Y軸移動量（2バイト）
        report[3] = y[0]; 
        report[4] = y[1];
        // ホイールスクロール量（2バイト）
        report[5] = w[0]; 
        report[6] = w[1];
        
        report
    }

    /// 相対移動量をリセットします。
    ///
    /// 同期イベント（SYN）を受信した後に呼び出し、
    /// 次のイベントバッチに備えて移動量をクリアします。
    /// ボタン状態はリセットしません（押しっぱなしの状態を維持）。
    fn reset_rel(&mut self) {
        self.move_x = 0;
        self.move_y = 0;
        self.scroll_y = 0;
    }
}

// =============================================================================
// プロキシ実行関数
// =============================================================================
/// マウスプロキシのメイン処理を実行します。
///
/// 入力デバイスからイベントを読み取り、HIDガジェットデバイスに
/// レポートとして転送します。
///
/// # 引数
/// * `device_path` - 入力デバイスのパス（例: /dev/input/event0）
/// * `output_path` - 出力デバイスのパス（例: /dev/hidg1）
///
/// # 戻り値
/// 処理が正常に完了した場合は`Ok(())`、エラーの場合は`Err`
///
/// # エラー
/// - 入力デバイスのオープンに失敗した場合
/// - 出力デバイスのオープンに失敗した場合
/// - デバイスの読み書き中にエラーが発生した場合
async fn run_proxy(device_path: &str, output_path: &str) -> Result<()> {
    // 入力デバイスをオープン
    let device = Device::open(device_path).context("Failed to open input device")?;
    let name = device.name().unwrap_or("Unknown");
    
    info!("Starting MouseProxy for {} ({}) -> {}", name, device_path, output_path);

    // 出力デバイス（HIDガジェット）をオープン
    // 読み書き両方のモードで開く必要がある
    let mut output_file = OpenOptions::new()
        .write(true)
        .read(true)
        .open(output_path)
        .context(format!("Failed to open output {}", output_path))?;

    // マウス状態を初期化
    let mut state = MouseState::new();
    
    // 入力デバイスを非同期イベントストリームに変換
    let mut input_stream = device.into_event_stream()?;

    // メインイベントループ
    loop {
        match input_stream.next_event().await {
            Ok(event) => {
                match event.kind() {
                    // === キーイベント（ボタン） ===
                    InputEventKind::Key(key) => {
                        // value: 1 = プレス, 0 = リリース, 2 = オートリピート
                        //
                        // hid-logitech-hidpp がキーボードコレクションを持つマウス
                        // (MX Master 3 の BLE 接続など) を Keyboard として登録すると
                        // EV_REP が有効になり、押しっぱなしのボタンに対して value=2 が
                        // 40ms 間隔で届く。これを「リリース」と解釈するとドラッグ中に
                        // ホストへ button=0 を送ってしまい、押し直すまで復帰しない。
                        // リピートは状態を変えず無視する。
                        let is_press = match event.value() {
                            1 => true,
                            0 => false,
                            _ => continue,
                        };

                        // ボタンに応じた状態を更新
                        // 各ボタンは異なるビット位置を持つ
                        match key {
                            Key::BTN_LEFT => state.btn_left = if is_press { 1 } else { 0 },
                            Key::BTN_RIGHT => state.btn_right = if is_press { 2 } else { 0 },
                            Key::BTN_MIDDLE => state.btn_middle = if is_press { 4 } else { 0 },
                            Key::BTN_SIDE => state.btn_side = if is_press { 8 } else { 0 },
                            Key::BTN_EXTRA => state.btn_extra = if is_press { 16 } else { 0 },
                            _ => {}  // その他のキーイベントは無視
                        }
                    }
                    // === 相対軸イベント（移動、スクロール） ===
                    InputEventKind::RelAxis(axis) => {
                        match axis {
                            // X軸移動（正: 右, 負: 左）
                            RelativeAxisType::REL_X => state.move_x = event.value() as i16,
                            // Y軸移動（正: 下, 負: 上）
                            RelativeAxisType::REL_Y => state.move_y = event.value() as i16,
                            // ホイールスクロール（正: 上, 負: 下）
                            RelativeAxisType::REL_WHEEL => state.scroll_y = event.value() as i16,
                            _ => {}  // その他の軸は無視（水平スクロールなど）
                        }
                    }
                    // === 同期イベント ===
                    // イベントバッチの終了を示す
                    // このタイミングでHIDレポートを送信
                    InputEventKind::Synchronization(_) => {
                        // 現在の状態からレポートを生成
                        let report = state.to_report();
                        
                        // HIDガジェットデバイスにレポートを書き込み
                        if let Err(e) = output_file.write_all(&report) {
                            error!("Failed to write report: {}", e);
                            break;  // 書き込みエラーでループを終了
                        }
                        
                        // 相対移動量をリセット（次のイベントバッチに備える）
                        state.reset_rel();
                    }
                    _ => {}  // その他のイベントは無視
                }
            }
            Err(e) => {
                // 入力デバイスの読み取りエラー（切断など）
                error!("Input device error: {}", e);
                break;
            }
        }
    }
    Ok(())
}

// =============================================================================
// メインエントリーポイント
// =============================================================================
/// プログラムのエントリーポイント
///
/// tokioランタイムを初期化し、プロキシを実行します。
/// エラーが発生した場合はエラーログを出力し、終了コード1で終了します。
#[tokio::main]
async fn main() -> Result<()> {
    // 環境変数からログレベルを設定（RUST_LOG環境変数を使用）
    env_logger::init();
    
    // コマンドライン引数をパース
    let args = Args::parse();

    // プロキシを実行
    if let Err(e) = run_proxy(&args.input_device, &args.output_device).await {
        error!("Proxy error: {}", e);
        std::process::exit(1);
    }
    Ok(())
}
