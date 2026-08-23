# 調査記録: MX Master 3 ドラッグ不安定 / プロジェクト改善点

記録日: 2026-08-12（Claude Code とのやり取りの記録）

## プロジェクト改善点一覧

### バグの疑いが強いもの

1. **Btn1+3 長押しシャットダウンが動かない可能性** — `src/keyboard_proxy.py:746`, `:804` が GPIO コールバック（gpiozero の別スレッド）内で `asyncio.create_task()` を呼んでいる。そのスレッドには実行中のイベントループがなく `RuntimeError` になるはず。他ハンドラ同様 `asyncio.run_coroutine_threadsafe()` に揃える。実機未検証。
2. **メールアドレス入力が JIS ホストで文字化けする疑い** — `send_email_address()`（`src/keyboard_proxy.py:704` 付近）は `@` を `Shift+KEY_2` で送る US 配列前提。リマップ機能は「ホストは JIS」前提（`@` は `Shift+2` → `KEY_LEFTBRACE`）であり矛盾。JIS ホストでは `@` が `"` になるはず。
3. **一時 Shift 処理でモディファイアが失われる** — `update_state()`（`src/keyboard_proxy.py:507-510`）で `is_shift_up` 時に `report[0] = 0x02` と上書きするため、Ctrl 等を押しながらリマップ対象キーを打つと Ctrl が一瞬落ちる。複数キー同時押し時はフラグがキーごとに立つのに 1 レポートに畳まれ挙動不定。
4. **Rust 側の相対移動量が上書きで消える可能性** — `main.rs:227-231` が SYN バッチ内の同軸 REL イベントを代入で上書き。`saturating_add` での加算が安全。

### 設計・性能

5. キーストロークごとに `/dev/hidgX` を `open()` し直している（`write_report()`、`src/keyboard_proxy.py:537`）。fd 保持に変更すべき。
6. `read_one` + `sleep(0.01)` のポーリング（`src/keyboard_proxy.py:338-341`）。evdev の `async_read_loop()` でレイテンシ・CPU 起床を改善できる。
7. 複数マウス時の `/dev/hidg1` 固定（`systemd/mouse-proxy@.service:8`）。`mouse_outputs` 複数指定が無意味。
8. キーボードの対象デバイス名正規表現がコード埋め込み（`device_monitor()`、`src/keyboard_proxy.py:830`）。config.json に出すべき。

### 整合性・保守性

9. 設定ファイル検索順の食い違い — Python は `/etc` → スクリプト横 → CWD、`setup_hid_gadget.sh:40-48` はスクリプト横 → `/etc`。
10. 実運用値が入る `config/config.json` が git 追跡下。追跡は sample のみにして gitignore すべき。
11. 死んだコード — `combination_check_delay`（読み込むだけで未使用）、`Button.was_held = False`（`button_states` に置換済みの残骸）。
12. `proxy_core.py:134,154` の `print()` デバッグ出力を logging に統一。
13. `asyncio.get_event_loop()`（`keyboard_proxy.py:891`）は Python 3.12 で非推奨。

### テスト・CI・運用

14. 自動テストがゼロ。`remap()`、`_deep_merge()`、`to_report()` は実機なしでテスト可能。
15. CI がない。shellcheck / cargo clippy / aarch64 ビルド / Python 構文チェック程度でも価値がある。
16. systemd ユニットのハードニング不足（`ProtectSystem=`、`DeviceAllow` 等なし、全て root）。
17. 水平ホイール非対応（対応するならディスクリプタ・`report_length`・`to_report()` の 3 点同時変更）。

## MX Master 3 ドラッグ不安定の調査

### 環境・症状

- Bluetooth マウス 2 台を Pi Zero 2W に接続（HHKB Studio トラックポイント + MX Master 3）。
- MX Master 3 のドラッグが不安定。**途中でボタンが離されたような挙動になり、その後再 press に相当する反応はない**（物理的に押し直すまで復帰しない）。
- **ポインタの移動は滑らかで、飛び・詰まりは観測されない**。
- **PC と Bluetooth 直結した場合は問題なし**（デバイス本体・デバイス側 BT 実装は白に近い）。
- MX Master のデバイスノードは 1 つだったと記憶（未再確認）。

### 棄却済み仮説

| 仮説 | 棄却根拠 |
|---|---|
| hidg1 共有による 2 インスタンス衝突（`mouse-proxy@.service` の `/dev/hidg1` 固定） | HHKB の電源を落としても症状が再現した |
| スイッチのチャタリング（MX Master 3 の既知問題） | PC 直結（BT）では問題なし。また release 後に即 press が再来しない点も不一致 |
| プロキシ再起動による状態リセット（`MouseState::new()` で全ボタン 0 → 最初の SYN で release 送出） | 再起動なら入力の空白（RestartSec 100ms〜、BT 再接続なら秒単位）でポインタが止まるはずだが、移動は滑らかなまま。※ NRestarts/journalctl での直接確認は未実施 |

### 現在の有力仮説（未確定）

**Pi 側の受信経路（BT リンク〜カーネル HID ドライバ `hid-logitech-hidpp`〜evdev）のどこかで release が一度だけ合成されている。**

- PC（Windows/macOS スタック）と Pi（BlueZ + Linux HID）で唯一違うのがこの層。
- 「再 press が来ない」は evdev のエッジトリガ性で説明できる: デバイスは button=1 のレポートを送り続けているが、HID ドライバ内部状態と食い違ったまま「変化なし」扱いになり press イベントが二度と出ない。Linux input コアには切断・サスペンド時に押下中キーの release を合成する挙動（`input_dev_release_keys`）がある。
- ポインタが滑らか・release は一度きり・押し直すまで復帰しない、という全観察と矛盾しない。

### 未実施の検証手順（優先順）

1. **evtest 併走**（プロキシは mouse を grab していないので並行観測可能）:
   ```bash
   sudo evtest /dev/input/eventX   # ドラッグ再現中に観測
   ```
   - ドロップ瞬間に `BTN_LEFT value 0` → release は evdev 層由来（プロキシ無罪）で確定
   - evtest 自体がエラー終了 → ノード消滅（再起動仮説の復活）
   - `SYN_DROPPED` → バッファ溢れ
   - 何も出ない → プロキシ〜ガジェット側
2. **hid-logitech-hidpp の切り離しテスト**（可逆）:
   ```bash
   lsmod | grep hidpp
   dmesg -T | grep -i -e hidpp -e logitech
   echo "blacklist hid_logitech_hidpp" | sudo tee /etc/modprobe.d/no-hidpp.conf
   sudo reboot   # 以後 hid-generic で動作。戻すのはファイル削除+再起動
   ```
   症状が消えれば hidpp ドライバ起因が確定。
3. **btmon** でドロップ瞬間のリンクイベント（切断・再接続・接続パラメータ更新）との相関確認。
4. 再起動仮説の白黒: `systemctl show 'mouse-proxy@*' -p NRestarts` と `journalctl -u 'mouse-proxy@*'`。
5. ノード数の再確認: `cat /proc/bus/input/devices`（`Logitech*` に複数 event ノードがマッチすると 1 台でも hidg1 衝突が起こり得る）。

### 構成上の制約（重要）

- **Pi Zero 2W の USB データポートは 1 つで、dwc2 peripheral モードとしてホスト PC への HID ガジェット接続に占有されている。Pi は USB ホストになれず、レシーバーを挿す場所はない。Bluetooth が唯一の入力経路。**
- `rfkill block wifi` テストは SSH 経路（WiFi）を自分で切ることになるため物理アクセス時のみ。
- Pi 4/5 なら USB-C を gadget にしつつ USB-A でホスト動作が併用でき、レシーバー案が成立する（ハード変更の選択肢）。

### 対応案

**案 1: マウスプロキシの入力を evdev → hidraw に変更（有力・恒久対策）**

- BT マウスは BlueZ → uhid → HID コアから hid-input/evdev と hidraw に分岐する。hidraw を読めば疑っている層（hid-input の状態管理・input コアの release 合成・evdev のエッジトリガ）を全て迂回できる。
- 本質的利点は**エッジ→レベルへのセマンティクス変更**: 毎レポートにボタン全ビットのスナップショットが入るため、1 レポート欠落・化けしても次のレポートで自動復帰する。「一度離れたら戻らない」故障モードがクラスごと消える。
- コスト: レポート解釈が必要（`HIDIOCGRDESC` でディスクリプタ取得して解析。対象 2 機種決め打ち+起動時検証の割り切りも可）。udev を `SUBSYSTEM=="hidraw"` に、テンプレートを `mouse-proxy@hidrawX` に変更。
- 事前確認: `ls /dev/hidraw*` と `grep -H . /sys/class/hidraw/hidraw*/device/uevent` で MX Master のノード存在を確認。
- 変形「ディスクリプタごとパススルー」（gadget の report_desc にデバイスのディスクリプタを書き、hidraw→hidg の単純パイプにする）は変換ロジックが消えるが、gadget ディスクリプタはブート時固定のため動的差し替えはホスト側 USB 再列挙を伴う。既知機種の事前焼き込みなら可。
- 限界: hidraw も BlueZ/uhid の下流なので「BlueZ がレポートを流さない」障害は防げない（ただしレベル方式なので復帰後に状態は合う）。

**案 2: hidpp blacklist を恒久策にする**（検証 2 で確定した場合。書き換えゼロ）

**案 3: BlueZ の GATT API で HOGP 直接受信** — 迂回範囲最大だが、hog プラグイン無効化が HHKB キーボードの evdev 経路も殺すため、今回の目的には過剰。

**併せて入れる小修正**: `main.rs` の REL 値を `saturating_add` に。起動時 `EVIOCGKEY` でボタン実状態を初期値に反映（evdev 継続の場合の再起動耐性）。

### 進め方

検証 1・2 で「release が evdev 層で生成されている」ことを確定させてから着手。blacklist だけで消えるなら書き換え不要。恒久策・再発時は案 1（hidraw 化）。

## キーボード側 hidraw 化の検討

マウスのドラッグ問題とは独立（効果なし）だが、キーボード単体として同種の改善が得られる。

- **同じ自己修復性**: キーボードレポートも「モディファイア + 押下中キー最大 6 個」の全量スナップショット。エッジ落ちによるキー張り付き/勝手離れがクラスごと消える（`reset_state()` はその対症療法）。
- **remap が簡潔になる**:
  - 入力が最初から HID usage コードになるため、evdev keycode → HID の逆変換テーブル `hid_keys.py`（316 行）が不要になる。remap は usage → usage の直接写像。
  - モディファイアとキーが同一レポートの原子的スナップショットで届くため、現行の Shift 依存リマップが持つイベント順序依存（改善点 #3 の一時 Shift 問題含む）が構造的に消える。
- **新たな問題: grab の喪失**。hidraw に grab 相当はなく、evdev ノード経由で打鍵が Pi の tty に漏れる。対策は「evdev ノードを開いて `EVIOCGRAB` だけ掛けて読み捨て、データは hidraw から読む」ハイブリッド。
- **レポート ID の分離**: HHKB Studio は BT 上 1 つの HID デバイスにキーボード・トラックポイント・メディアキーが多重化されているはず。hidraw では 1 ノードに全部届くため ID で選別。hidraw は hid-input との並行タップなので、トラックポイントを evdev のまま Rust プロキシに任せる併用は可能。
- **ディスクリプタ依存**: 6KRO 標準形か NKRO ビットマップかは実機の `HIDIOCGRDESC` を見るまで不明。NKRO ならビットマップ→6 キー配列変換が 1 段入る。
- 判断: キーボードでは症状未発生のため緊急性はない。マウス側 hidraw 化で方式の実績を作ってから同方式に揃えるのが低リスク。

## 実機検証結果（2026-08-23、Geirrod.local、MX Master 3 単独）

### 接続経路で判明した事実

- 起動直後の 3 回の BT 接続で、BlueZ 5.66 が読んだ HID Report Map が **88 バイトで途切れ**、`hid-generic` / `hid-logitech-hidpp` とも `item fetching failed at offset 87/88` で probe 失敗 → 入力デバイスが生成されない状態だった。ATT MTU は 23（デバイス側主張）で、88 = 22 × 4 ブロック。BlueZ は Report Map をディスクキャッシュせず bluetoothd 起動後初回の読み取り結果をメモリで使い回すため、再接続しても復帰しない。bluetoothd 再起動後の接続（`le-connection-abort-by-local` 4 回の後に成功）で 140 バイト完全読み取りに成功し、`Logitech Wireless Mouse MX Master 3`（event0 / hidraw0）が生成された。
- Report Map の構成: Report ID 1 = キーボード、Report ID 2 = マウス（ボタン 16bit、X/Y 12bit、ホイール 8bit、AC Pan 8bit）、Report ID 0x11 = HID++ ベンダ 19 バイト。
- `hid-logitech-hidpp` はこのデバイスを **`Keyboard`** として登録する（dmesg: `BLUETOOTH HID v0.13 Keyboard [Logitech Wireless Mouse MX Master 3]`）。そのため EV_REP が有効になる。
- 調査中に Pi の WiFi（SSH）が 2 回落ちた。Zero 2W の WiFi/BT 共有無線の不安定さは実在する。

### ドラッグ試験（evtest + btmon 併走）

- evdev 層に **`BTN_LEFT value 2`（オートリピート）が press の約 250ms 後から 40ms 周期で連続**して届く。
- press 16 回 / release 16 回で一致。`SYN_DROPPED` 0。BLE の切断・再接続・Connection Update 0。`mouse-proxy@` の NRestarts 0。

### 原因（確定）

`rust/mouse_proxy_rs/src/main.rs` のボタン処理 `let is_press = event.value() == 1;` が value=2（リピート）を「リリース」として扱い、リピートのたびにホストへ button=0 を送信していた。押下 250ms 後にドラッグが切れ、物理的に押し直すまで press イベントが来ないため復帰しない。ポインタ移動は影響を受けない。PC 直結で正常なのは PC 側 HID スタックにこのオートリピートが無いため。HHKB トラックポイントは純粋なマウスノードで EV_REP を持たないため影響なし。

棄却済みの各仮説（衝突・チャタリング・再起動・BT 層での release 合成）はいずれも実測で否定された。hidraw 化は本件の解決には不要。

### 修正

value=2 を状態変更なしで無視する（`match event.value() { 1 => press, 0 => release, _ => continue }`）。Geirrod.local / Kiviuq.local に配置済み。
