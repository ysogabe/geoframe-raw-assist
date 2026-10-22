# 設計書: プリセット値の Core Image 向け再調整と `highlight_recovery` の写像（Issue #81）

- 対象: 11 プリセット（`src-tauri/src/presets.rs`）の値を `apple_default`・decoder 9・レンズ補正 ON（ADR 0002 の基本路線）向けに再調整し、
  CLI で no-op になっている `highlight_recovery` を `exposure` と `shadow_lift` への写像で置き換える。
- 前提（確定済み）: #92 の判定 A（[診断レポート](../coreimage-highlight-diagnosis-report.md) §7〜§8、[ADR 0002 追補](../adr/0002-coreimage-engine-decision.md)）。
  合否の基準は **CI 既定（`d_base`）の出力**。採用候補は C1' = `exposure −0.7 + shadow_lift 40（brightness 1.0）`、強め `−1.0 + 55`。
  `brightness > 1` での戻し・`CIHighlightShadowAdjust`・gamut OFF + 自前カーブ・EDR / boost は使わない。`noise_reduction` の表は T5 で確定済みで触らない。
- 関連: [対応表](../coreimage-parameter-mapping.md)「#73 での決定」、[プレビュー調整の設計書](coreimage-preview-adjustments.md) §3.3、
  [比較レポート](../coreimage-engine-comparison.md) §13・§16・§17、[機能移行の設計書](coreimage-feature-migration.md) §3.1、[契約](../API_CONTRACT.md) §2 / §5 / §9。
- 本書はコードを含まない。実装は §9 のタスクに分けて進める。撮影地・位置情報は書かない。

## 表記

- **【事実】** = コード・計測結果・公開 API の挙動で確認したこと（出典を併記）。**【推測】** = 未計測の見立て。実装・計測で確定させる。
- `hr` = `highlight_recovery`（0..100）。`ΔEV(hr)` / `Δlift(hr)` = 写像が足す露出（EV）と `shadow_lift`（0..100 のスライダー単位）。
- 「実効値」= プリセット自身の値に写像の寄与を合成した後、CLI が実際に CIRAWFilter / 後段へ書く値。

## 0. 要点（先に結論）

| 論点 | 決定（推奨） |
|---|---|
| 写像の形（§2） | 区分線形。節点 `(0: 0, 0) → (60: −0.7, 40) → (90: −1.0, 55) → (100: −1.1, 60)`。**hr 0 は恒等**。100 の節点は外挿【推測】でハーネスで確かめる。置き場所は **CLI（Swift）**。`adjustments_applied.highlight_recovery` に記録し、`ignored` から外す |
| 合成（§3） | **加算**。`exposure_eff = exposure + ΔEV(hr)`、`lift_eff = clamp(shadow_lift + Δlift(hr), 0, 100)`。max / 置換はスライダーに不感帯を作るので採らない |
| 既定 hr の見直し（§4） | **非救済 8 プリセットの hr を 0 にする（案 B）**。今の値（55〜65）を残すと全プリセットが CI 既定より −0.64〜−0.75EV 寄りになり、オーナーが目視で承認した `apple_default` の画から外れる。hr > 0 は `highlight_rescue` / `backlit_rescue` / `sunset_glow` だけ。軽い hr（30）を `neutral_recovery` / `landscape_clear` に入れる案 C はシートで判断（オーナー確認 1・5） |
| `highlight_recovery` の存廃（§5） | **残す**。廃止は CLI の 9 キー契約・フィクスチャ構造・Python（rawpy は hr が要る）に波及する。残せばスライダーが効くようになり UI の注記を外せる |
| フィクスチャ（§6） | **2 本に分ける**。`src-tauri/tests/fixtures/presets.json` = CI 値（正は Rust。Rust と TS が読む）、`tests/fixtures/presets_legacy_rawpy.json` = rawpy 値（Python が書き出し・読む）。数値以外（name / label / description / 順序 / キー構成 / 値域 / NR 選択肢）の一致は Python のテストで両ファイルを突き合わせて守る |
| 再調整の手順（§7） | ハーネスに `--presets` モードを足し、#92 の 10 カット + シーン別 8 カットで「hr の寄与（同じプリセットの hr 0 比）」と「プリセットの個性（`d_base` 比、宣言した方向）」を分けて測る。反復は最大 3 回 |
| 完了条件（§7.4） | Issue 本文の「rawpy との ΔE」「白飛び率が rawpy 以下」は無効。**「救済系 3 プリセットで白飛び率が CI 既定より下がり、中間調が CI 既定 ±0.03 以内（意図した差があるプリセットは差の方向を宣言して確認）」**に改める |
| 変更の範囲（§9） | CLI 0.5.0 → **0.6.0**、Rust のプリセット値、フィクスチャ分割、フロントの注記と hint、ハーネス、契約・対応表・CLI README・ADR 0002 追補。`CACHE_KEY_SCHEMA` は**上げない**（鍵に CLI 版と実行ファイルの SHA が入っている【事実】） |

## 1. 現状の把握【事実】

| 項目 | 現状 | 出典 |
|---|---|---|
| プリセット表の正 | Rust `presets.rs: PRESETS`（11 件、`numbers: [f64; 8]` + `noise_reduction`）。Python `local_raw/presets.py: PRESETS` は同じ値。一致は `src-tauri/tests/fixtures/presets.json` で Rust / Python / TS の 3 テストが担保 | `presets.rs` 冒頭、`tests/test_presets_fixture.py`、`src/lib/format.test.ts` |
| フィクスチャの生成 | Python の `scripts/export_presets_fixture.py --write` が `presets.py` から書き出す。Rust のテストは `include_str!` で読んで `Value` 比較（15 と 15.0 を区別） | `export_presets_fixture.py`、`presets.rs` のテスト |
| TS のテストが読む範囲 | `adjustment_ranges` だけ（`SLIDER_SPECS` の min / max）。プリセット値は読まない | `format.test.ts` |
| プレビューの経路 | フロントが `get_presets`（Rust）の値に `state.overrides`（触ったキーだけ）を重ねて 9 キー全量を `develop_preview_coreimage` へ渡す。Rust は `normalize_adjustments` で検証し CLI へそのまま送る | `App.tsx:561`、`app-state.ts: resolveAdjustments`、`coreimage.rs: base_request` |
| 本現像・書き出しの経路 | `POST /api/raw-develop` に **preset 名 + overrides だけ**を送り、Python が `presets.py` の値で解決する（rawpy） | `App.tsx:577`、`raw-api.ts:151` |
| CLI の `highlight_recovery` | 受理するが効かせない。`Adjustments.ignoredKeys = ["highlight_recovery"]` を応答 `adjustments_applied.ignored` と `--capabilities` に載せる | `Adjustments.swift`、`Develop.swift: applyAdjustmentsDecoder` |
| `exposure` の写像 | `filter.exposure = ev`（1:1、固定 baselineExposure 基準）。CLI は −5..5 を受け、Rust が −2..2 にクランプ | `Develop.swift`、`adjustments.rs` |
| `shadow_lift` の写像 | `lift = v / 100` を Metal `geoframe_tone` で `x + lift·2·x·(1−x)²`（ピーク x ≈ 1/3、x = 1 で 0。白側を動かさない）。PostChain には `shadowLift / 100.0` が**クランプ無しで**渡る | `Develop.swift:305`、`PostChain.swift` |
| `wb_temp` の写像 | mired シフト（`MIRED_PER_UNIT = 0.85`）。T5 で rawpy の ±60 と大きさが揃うことを確認（比 0.74〜1.24、中央値 ≈ 1.0） | 比較レポート §16.4 |
| 後段（b / c / s / USM） | rawpy と同じ式を sRGB ガンマ空間で適用（オラクル比較 ΔE76 中央値 0）。ただし入力は `apple_default` の出力で、rawpy 出力とは明るさ・トーン・彩度が違う | 対応表「#73 での決定」、比較レポート §16.1 |
| C1' の計測値 | preview 10 カット中央値: clip8 `d_base` 13.0% → C1'(−0.7) 3.6% / 強め 2.2%。`mid_p50` `d_base` 比 −0.011 / −0.012（10/10 と 9/10 が ±0.03 以内）。`fg_p01` −0.019 / −0.020。magenta 0（preview）。原寸は DSC04607 で 0.0235（閾値 0.01 を超える） | 診断レポート §5.1 |
| C1' の rawpy 比 | rawpy 現行（`neutral_recovery`、auto_bright）はカメラ比 −0.128〜−0.160 暗い。CI を同じ p50 に合わせると（≈ −1.0EV）白飛びは rawpy より少ない | 比較レポート §13.5、診断レポート §5.1 |
| キャッシュ鍵 | `schema` + リクエスト全量 + CLI `--version` 応答 + 実行ファイル SHA-256 + ARW のサイズ / mtime | `coreimage.rs: cache_key` |
| フロントの応答 | `toDevelopedResult` は `adjustments_applied` をフロントへ渡していない。`BeforeAfter.tsx:157` は hr > 0 のとき固定文で「適用されません（#81 で対応予定）」と注記 | `coreimage-api.ts`、`BeforeAfter.tsx` |
| ハーネス | `--adjustments`（識別 identity = hr 0、オラクル `or_preset` は Python の既定プリセット）、`--highlight`、`--diagnosis`（ROI `midtone` / `fg_dark` / `sky_bright` / `cloud_grad`、C1' の仕様 `C1_SPECS`、合否 `MID_P50_TOL 0.03` / `FG_P01_DROP_MAX −0.02` / `MAGENTA_RATE_MAX 0.01`）。プリセット単位で CI を回すモードは無い | `scripts/compare_engines.py` |

## 2. `highlight_recovery` → (`exposure`, `shadow_lift`) の写像

### 2.1 形

区分線形。節点は次の 4 点。60 と 90 は #92 で計測・承認済みの C1'（【事実】）、0 は恒等（設計上の要請）、100 は 60→90 の傾きで外挿した値（【推測】。§7 で計測する）。

| hr | ΔEV(hr) | Δlift(hr) | 根拠 |
|---|---|---|---|
| 0 | 0 | 0 | 恒等。ハーネスの `ADJ_IDENTITY`・オラクル・回帰ベースラインは hr 0 が無効果である前提【事実】 |
| 60 | −0.7 | 40 | C1'（診断レポート §5.1、10/10） |
| 90 | −1.0 | 55 | C1' 強め（同、9/10） |
| 100 | −1.1 | 60 | 外挿【推測】。90 で止めるとスライダーの 90..100 が不感帯になる |

節点間は線形補間。**節点では表の値をそのまま返す**（補間演算を通さない。`60 × (40/60)` の丸めで 40.0 が崩れると §2.2 の等価性が成り立たない）。
参考値: hr 30 → (−0.35, 20)、50 → (−0.583, 33.3)、55 → (−0.642, 36.7)、70 → (−0.8, 45)、80 → (−0.9, 50)。

- 1 本の直線 `0 → 100` にしない理由: 2 つの承認済み節点を同時に通らない（60 で −0.66 / 36、90 で −0.99 / 54 になる）。承認値を厳密に再現する方を取る。
- 0..60 の傾き（−0.0117 EV / 単位、0.667 lift / 単位）と 60..90 の傾き（−0.01、0.5）はほぼ同じで、折れ目は目に見えない【推測】。
- 比 `Δlift / |ΔEV|` は 57〜55 で一定に近い。「露出を下げた分を lift で中間調へ戻す」という C1' の構造を全域で保つ。

### 2.2 契約として書く等価性

`{exposure: 0, shadow_lift: 0, hr: 60}` と `{exposure: −0.7, shadow_lift: 40, hr: 0}` は**同じ画**（CLI の `applied.exposure` と `post.shadow_lift` が同じ、レンダ結果が一致）でなければならない。90 ↔ (−1.0, 55) も同様。
Swift の単体テスト（合成後の実効値）と、ハーネスの 1 チェック（2 つのリクエストの tiff16 が一致）で縛る。これで実装が式ではなく**承認済みの計測**に結び付く。

### 2.3 置き場所: CLI（Swift）

| 候補 | 利点 | 欠点 | 判定 |
|---|---|---|---|
| **CLI** `applyAdjustmentsDecoder` の前で合成し、exposure はデコード段・lift は後段へ | `wb_temp` の mired 写像と同じ場所（CI 固有の解釈は CLI に集約）。ハーネスが CLI 直叩きで検証できる。Rust はエンジン非依存のまま。応答 `adjustments_applied` に実効値を記録できる | CLI 版を上げる（0.6.0） | **採用** |
| Rust で実効値を計算して CLI に渡す | CLI 無変更 | CLI は hr も受けるので二重適用を避けるため hr を 0 に書き換えて送る必要がある。キャッシュ鍵のリクエストと UI の値がずれる。ハーネス（Python）が Rust の計算を再現しないと検証できない | 不採用 |
| フロント | 最短 | クランプ・検証をサーバ側に置く契約と逆。Rust / CLI の検証が二重になる | 不採用 |

実装の位置（`Develop.swift: develop()`）: 既定値の読み戻しの直後に `hr → (ΔEV, Δlift)` を求め、`applyAdjustmentsDecoder` には `exposure_eff` を、`PostChainParameters.lift` には `lift_eff / 100`（[0, 1] に**明示クランプ**）を渡す。
応答 `adjustments_applied.highlight_recovery = {value, exposure_delta, shadow_lift_delta, effective_exposure, effective_shadow_lift, shadow_lift_clamped}` を追加し、`ignored` は**空配列**にする（フィールドは残す。Rust のテストと `--capabilities` が読む）。

## 3. プリセット自身の `exposure` / `shadow_lift` との合成

| 規則 | highlight_rescue（旧値 −0.3 / 15 / hr 90）での実効値 | スライダーの直観 | 判定 |
|---|---|---|---|
| **加算** `exposure + ΔEV`、`min(100, lift + Δlift)` | (−1.3, 70) | 3 本とも純粋なオフセット。`shadow_lift` を動かせば必ず暗部が動き、`exposure` を動かせば必ず全体が動く。rawpy 時代も hr は他と直交していた | **採用** |
| max `min(exposure, ΔEV)`、`max(lift, Δlift)` | (−1.0, 55) | hr 60 のとき `shadow_lift` を 0〜40 で動かしても何も変わらない（不感帯） | 不採用 |
| 置換（hr > 0 なら写像値、プリセットの値を捨てる） | (−1.0, 55) | hr > 0 で `exposure` / `shadow_lift` スライダーが死ぬ | 不採用 |

- クランプ: `lift_eff` は [0, 100]（後段カーネルは lift 1.0 までオラクルで確認済み【事実】`or_lift_100`）。`exposure_eff` は Rust のクランプ後 −2..2 + ΔEV ≥ −1.1 で −3.1..2 に収まり、CLI の −5..5 の内側なので追加のクランプは不要（`shadow_lift_clamped` だけ記録する）。
- 加算の帰結は**プリセット値側で吸収する**（§8）。旧値のままだと `highlight_rescue` が −1.3EV、`backlit_rescue` が lift 100（50 + 50）になるので、救済系の `exposure` / `shadow_lift` を下げる。
- 見え方の注意: スライダーは `shadow_lift 15` を示すが画は lift 55 で描かれる。フロントは `adjustments_applied` を受けていないので、実効値を出すなら `toDevelopedResult` に `highlight_recovery` の実効値を通す小改修が要る（任意。オーナー確認 8）。

## 4. 既定 hr の見直し（重要な帰結）

加算写像を今の表にそのまま当てると、11 プリセットすべてが hr 55〜90 を持つので**全プリセットが CI 既定より −0.64〜−1.3EV 寄り**になる。

| プリセット | 旧 hr | 旧値のままの実効 (EV, lift) |
|---|---|---|
| neutral_recovery | 60 | (−0.70, 55) |
| landscape_clear / vivid_travel | 55 | (−0.64, 46.7) |
| nightscape | 55 | (−0.64, 51.7) |
| sunset_glow | 70 | (−0.90, 53) |
| indoor_warm | 55 | (−0.54, 54.7) |
| portrait_soft | 60 | (−0.70, 52) |
| monochrome_street | 60 | (−0.70, 50) |
| highlight_rescue | 90 | (−1.30, 70) |
| shadow_rescue | 65 | (−0.75, 87.5) |
| backlit_rescue | 80 | (−0.90, 100) |

問題点:

- ADR 0002 でオーナーが「好ましい」と目視したのは **`apple_default` の既定（`d_base`）そのもの**。C1' が承認されたのは「明るい空が主役の 10 カット」で**ハイライト救済の見え方**としてであり、全プリセットの基準にすることは未説明【事実: 診断レポート §8】。
- rawpy 時代の hr 60（`Blend`）は明るさを変えない方式だったが、CI の写像は露出を下げる。意味が「再構成方式の選択」から「露出をハイライトの余白と交換するレバー」に変わる。
- `shadow_rescue`（暗部を持ち上げる）と `nightscape`（夜景）に −0.64〜−0.75EV が入るのは目的と逆向き。lift の上積みは高 ISO でノイズを増やす【推測】。
- 空の無いシーン（室内・人物・夜景）では白飛びの戻しが無く、露出を下げて lift で戻す副作用（ハイライトの密度低下・暗部ノイズ）だけが残る【推測】。

| 案 | 内容 | 利点 | 欠点 |
|---|---|---|---|
| A | hr の値を変えない | プリセット編集ゼロ | 上記。承認済みの `d_base` の画から全プリセットが外れる。空の無いシーンで未検証 |
| **B（推奨）** | **非救済 8 プリセットの hr を 0** にし、hr > 0 を `highlight_rescue` / `backlit_rescue` / `sunset_glow` に限る | 8 プリセットは「CI 既定 + 後段の個性」になり、オーナーが承認した画を基準に保てる。救済系だけが C1' の構造を持つ | `neutral_recovery` の空の白飛び（中央値 13.0%）は CI 既定のまま残る |
| C | B + `neutral_recovery` / `landscape_clear` に軽い hr（30 → −0.35EV, lift 20） | 空の白飛びを半分程度に減らせる見込み（−0.3EV 単独で 13.0 → 8.7%【事実: 比較レポート §17.3】） | 未計測の組み合わせ。既定プリセットが CI 既定から外れる。シートで判断が要る |
| D | 写像を縮めて 60 を「軽い」値にする（例: 100 → (−1.0, 55)） | 表を変えずに済む | 承認済みの節点（60 → −0.7 / 40）を捨てることになり、§2.2 の等価性が失われる。全プリセットが暗くなる構造は変わらない |

**推奨は B。C はシートで `neutral_recovery` の hr 0 と 30 を並べ、オーナーが選ぶ**（オーナー確認 1・5）。

## 5. `highlight_recovery` を Adjustments に残すか

| 観点 | 残す（推奨） | 廃止する |
|---|---|---|
| CLI | 写像の追加と応答フィールド 1 つ。9 キー契約は不変 | 9 キー必須 → 8 キー。`RequestParser`・`numericKeys`・テスト・README・`--capabilities` の変更 |
| Rust | `ADJUSTMENT_RANGES` 不変。プリセット値だけ | `[f64; 8]` → 7、`ADJUSTMENT_RANGES`、`adjustments.rs` のテスト、`coreimage.rs` のモック |
| Python | 不変（rawpy は hr で `HighlightMode` を選ぶので**必須**） | 残さざるを得ない → Python と Rust のキー構成が割れ、フィクスチャの構造比較が成り立たない |
| フロント | `types.ts` のコメント、`format.ts` の hint、`BeforeAfter.tsx` の注記削除 | `types.ts` / `SLIDER_SPECS` / `ADJUSTMENT_LABELS` / `RECIPE_KEYS` / テスト。Python 経路（本現像）には hr を送れなくなる |
| 利用者 | 「ハイライト回復」スライダーが CI で効くようになる | スライダーが消える。rawpy 書き出しだけ hr を持つ状態は説明しづらい |

**残す**。将来 rawpy を取りやめた後（#90）に名前やヒントを見直す余地はあるが、本 Issue では触らない。

## 6. フィクスチャ整合テストの扱い

Rust の値を CI 向けに変え、Python を rawpy 値のまま残すと、現在の 1 本のフィクスチャでは Rust か Python のテストが必ず落ちる【事実: `presets.rs` と `test_presets_fixture.py` が同じファイルの `presets` を比較】。

| 案 | 内容 | 判定 |
|---|---|---|
| **分割（採用）** | `src-tauri/tests/fixtures/presets.json` = **CI 値**（正は `presets.rs`。Rust のテストが `Value` 比較、TS のテストが `adjustment_ranges` を読む）。`tests/fixtures/presets_legacy_rawpy.json` = **rawpy 値**（`export_presets_fixture.py --write` が `presets.py` から書き出し、`test_presets_fixture.py` が読む） | 2 つの正（Rust / Python）がそれぞれ自分のフィクスチャを持つ。役割が明確 |
| 1 本に 2 節（`presets` / `presets_legacy_rawpy`） | ファイルは 1 本 | 生成元が Python と Rust に割れて `--write` だけでは作れない。`--check` の意味も曖昧 |
| Python の値も CI 値に揃える | 分割不要 | rawpy の本現像の見え方が変わる（hr 0 → `Clip` で `Blend` を失う）。rawpy 取りやめ前に rawpy の挙動を変える理由が無い |

分割後の規則:

- **CI 側の生成**: Rust のテストモジュールに `#[ignore]` の書き出しテスト（例: `UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored`）を足し、`serde_json::to_string_pretty` で `presets.json` を書く（f64 は `15.0` と出るので TS / Python の float 要件と両立）。手で編集してもよい（Rust のテストが一致を検証する）。
- **legacy 側**: `export_presets_fixture.py` の `FIXTURE_PATH` を `tests/fixtures/presets_legacy_rawpy.json` に変え、docstring を「rawpy（Python）の表の写し」に改める。`--check` はそのまま。
- **構造の一致テスト（Python、新規）**: 2 本のフィクスチャを読み、`presets[].name / label / description` と並び、各 `adjustments` のキー集合、`adjustment_ranges`、`noise_reduction_choices` が**同一**であることを確かめる（数値だけ違ってよい）。契約 §5 の「キー構成と name は凍結」をこれで守る。
- `format.test.ts` は `presets.json`（CI）を読み続ける（値域は変わらない）。
- ハーネスのオラクル `or_preset` と新しい `--presets` モード（§7）は **`presets.json`（CI 値）を読む**（Python の `presets.py` ではない）。
- ブラウザモード（vite 単体）は `/api/health` の `presets`（Python の legacy 値）を使い続ける。ブラウザモードの現像は rawpy なので、これは正しい組み合わせ。

## 7. 11 プリセットの再調整手順

### 7.1 数値の意味が変わる点【事実】

| キー | rawpy での意味 | CI での意味 | 値の持ち越し |
|---|---|---|---|
| `exposure` | auto_bright 正規化後の `bright = 2**ev`（`neutral_recovery` はカメラ比 −0.13 暗い基準） | 固定 baselineExposure 基準（CI 既定はカメラ比 +0.008）。±1EV の大きさは rawpy 比 0.81〜1.18 | **基準が変わる**。救済系の −0.3 はすでに暗い基準の上の値だったので、CI では写像の ΔEV と二重になる → 0 にする |
| `wb_temp` | R/B ゲイン比（±0.3% / 単位） | mired シフト（0.85 / 単位）。±60 の大きさは rawpy と同等に較正済み | **そのまま持ち越せる**（方向・大きさをシートで確認） |
| `shadow_lift` / `brightness` / `contrast` / `saturation` | 表示参照の式 | 同じ式（オラクル一致）。ただし入力が `apple_default`（boost トーン入り・彩度高め） | 式は同じでも**効きが強く見える可能性**【推測】。シートで 1 段弱い候補も並べる |
| `sharpness` | 1.0 = 撮影時シャープ（rawpy 基準） | 1.0 = `apple_default` のデコーダ内シャープ（カメラ比エッジ幅 0.84、rawpy 現行 1.16 より鋭い）。> 1 は増分 USM | **> 1 は rawpy 時代より強く出る**【推測】。`landscape_clear` 1.15 は要確認 |
| `noise_reduction` | FBDD 3 段 | 確定済みの表（スコープ外） | そのまま |
| `highlight_recovery` | `Clip / Blend / Reconstruct` の選択（明るさ不変） | §2 の写像（露出を下げ lift で戻す） | **§4 のとおり見直す** |

### 7.2 初期案（表）

案 B を前提にした初期値。太字が旧値からの変更。右端は実効値。すべて**【推測】**で、§7.3 の計測とシートで確定する。

| name | exposure | wb_temp | shadow_lift | hr | brightness | contrast | saturation | sharpness | NR | 実効 (EV, lift) | 変更の理由 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| neutral_recovery | 0.0 | 0 | 15 | **0**（案 C: 30） | 1.0 | 1.03 | 1.02 | 1.04 | light | (0, 15)（案 C: (−0.35, 35)） | §4 |
| landscape_clear | 0.0 | 0 | 10 | **0**（案 C: 30） | 1.0 | 1.10（弱め候補 1.06） | 1.08（1.05） | 1.15（1.08） | light | (0, 10) | §4。後段の効きが強く出る見込み |
| nightscape | 0.0 | −3 | 15 | **0** | 1.0 | 1.04 | 1.0 | 1.02 | full | (0, 15) | 露出を下げる理由が無い。lift の上積みは高 ISO でノイズ増 |
| sunset_glow | **0.0**（旧 −0.1） | 12 | 8（run2 で **4** に確定） | **60**（旧 70） | 1.0 | 1.05 | 1.07 | 1.03 | light | (−0.7, 48) → 確定 (−0.7, 44) | 「空の階調を守る」= C1' 標準。旧 −0.1 は写像の ΔEV と二重。lift 8 では条件 5 の中間調が 3 カットで +0.032〜0.036 と外れたので 4 に下げた |
| indoor_warm | 0.1 | 5 | 18 | **0** | 1.0 | 1.02 | 1.03 | 1.02 | light | (+0.1, 18) | 窓の白はセンサー飽和で hr は効かない【事実: 診断 §4.4】 |
| portrait_soft | 0.0 | 3 | 12 | **0** | 1.0 | 0.98 | 1.0 | 0.96 | light | (0, 12) | §4 |
| monochrome_street | 0.0 | 0 | 10 | **0** | 1.0 | 1.08 | 0.0 | 1.04 | light | (0, 10) | §4 |
| highlight_rescue | **0.0**（旧 −0.3） | 0 | **0**（旧 15） | 90 | 1.0 | 1.0 | 1.0 | 1.02 | light | **(−1.0, 55) = C1' 強め** | 計測済みの構成を実効値にする（9/10）。旧値のままだと (−1.3, 70) |
| shadow_rescue | 0.0 | 0 | 45 | **0** | 1.0 | 1.0 | 1.0 | 1.01 | full | (0, 45) | 暗部救済に露出低下は逆向き |
| backlit_rescue | 0.0 | 0 | **30**（旧 50） | **60**（旧 80） | 1.0 | 1.0 | 1.02 | 1.02 | light | (−0.7, 70) | C1' 標準 + 被写体向けの追加 lift 30。旧値のままだと lift 100（頭打ち） |
| vivid_travel | 0.0 | 0 | 10 | **0** | 1.0 | 1.07 | 1.10 | 1.05 | light | (0, 10) | §4 |

`label` / `description` / `name` / 並びは凍結のまま変えない（契約 §5）。

### 7.3 計測（ハーネス `--presets` モード、新規）

- カット: #92 の 10 カット（空が主役。`highlight1` / `diagnosis` と同じ ID）+ シーン別の代表カット: 日中 DSC04670 / DSC04651 / DSC04872、室内・高 ISO DSC04430 / DSC04453（§16 と同じ）、夜景・星空 DSC07415 / DSC07407 / DSC07402（別フォルダ、§10 と同じ）。薄明（DSC04532）・室内の窓（DSC04432 / 04462）・逆光（DSC04473 / 04607 / 04746）は 10 カットに含まれる。
  **夕景の太陽そのものを含むカットは無い**（#92 で追加撮影しない判断）。`sunset_glow` の hr は代表カット無しで決まる（リスク §10）。
- バリアント（preview 2400px、`apple_default` v9 lens ON）: `d_base`（identity）、11 プリセット（`presets.json` の CI 値）、hr > 0 のプリセットについて **同じ値で hr = 0 にしたもの**（`<preset>_hr0`）、参考に rawpy の legacy プリセット（`worker_rawpy`、明るさの差を注記）。原寸は救済系 3 プリセット × 3 カット（DSC04473 / 04607 / 04705）だけ。
- 指標: `image_metrics` の `clip_high_any` / `p01` / `p50` / `p99` / `mean_saturation` / `flat_noise`、`clip_boundary_magenta_rate`、ROI（`diagnosis_rois` を流用: `midtone` の p50、`fg_dark` の p1、`sky_bright` / `cloud_grad` の clip8）、`d_base` 比の ΔE76、`rb_log_ratio`（WB の方向）。
- 比較の組を 2 つに分ける:
  1. **hr の寄与** = プリセット vs 同じプリセットの hr 0（自己基準）。プリセットの個性（彩度・WB・コントラスト）を打ち消して hr だけを見る。
  2. **プリセットの個性** = プリセット vs `d_base`。意図した差を**方向で宣言**し（下表）、`direction_match` で確認する。大きさは目視。

| プリセット | 宣言する差（`d_base` 比） | 確認する指標 |
|---|---|---|
| sunset_glow | 暖色、彩度 +、ハイライトの白飛び − | `rb_log_ratio` +、`mean_saturation` +、clip8 − |
| indoor_warm | 明るさ +、やや暖色 | `mean_luma` +、`rb_log_ratio` + |
| vivid_travel / landscape_clear | 彩度 +、コントラスト + | `mean_saturation` +、`std_luma` + |
| monochrome_street | 彩度 0 | `mean_saturation` ≈ 0 |
| portrait_soft | コントラスト −、シャープ − | `std_luma` −、`edge_width_ratio` ≥ 1 |
| nightscape / shadow_rescue | 暗部 +（`fg_p01` +）、NR full | `dark_mean_luma` +、`sky_noise` light 比 ≤ 0.9 |
| highlight_rescue / backlit_rescue | 白飛び −、中間調は ±0.03 | clip8 −、`mid_p50_diff` |
| neutral_recovery（hr 0） | ほぼ `d_base`（後段の 1.03 / 1.02 / 1.04 のみ） | ΔE76 中央値が小さいこと（報告のみ） |

### 7.4 合否（完了条件の改訂）

Issue 本文の「rawpy 出力との ΔE76」「救済系の白飛び率が rawpy 以下」は、rawpy の取りやめと `d_base` 基準の判断に合わないので**無効**。次に改める（オーナー確認 2）。

| # | 条件 | 対象 | 判定 |
|---|---|---|---|
| 1 | hr の寄与: `clip8_any(P) < clip8_any(P_hr0)`（P_hr0 の clip8 が 1% 未満のカットは n/a） | hr > 0 の 3 プリセット × 10 カット（preview） | 該当カットすべて |
| 2 | hr の寄与: `abs(mid_p50(P) − mid_p50(P_hr0)) ≤ 0.03`（`midtone` ROI。取れないカットは全画面 p50） | 同 | 9 / 10 以上 |
| 3 | hr の寄与: `fg_p01(P) − fg_p01(P_hr0) ≥ −0.02` | 同 | 9 / 10 以上 |
| 4 | 偽色: `magenta_rate ≤ 0.01`（preview）。原寸は数値を記録し**目視のみ**（原寸の閾値は #92 の申し送りどおり未確定） | 同 | preview で該当カットすべて |
| 5 | `d_base` 比: 救済系 3 プリセットで `clip8_any(P) < clip8_any(d_base)` かつ `abs(mid_p50 − d_base) ≤ 0.03`。**`backlit_rescue` は中間調の幅を問わない**（被写体向けの lift 30 で中間調を上げる設計。オーナー判断 #3。hr の寄与は条件 2 で見る）。`d_base` の clip8 が 1% 未満のカットは n/a（条件 1 と同じ。run1 の後に追加） | highlight_rescue / backlit_rescue / sunset_glow（sunset は彩度・WB の差があるので `mid_p50` は L で見る） | 8 / 10 以上 |
| 6 | 個性の方向: §7.3 の宣言どおり。clip8 の方向は `d_base` の clip8 が 1% 未満のカットで n/a（run1 の後に追加） | 全プリセット × 日中 3 + 代表 | 宣言した方向に一致（大きさは目視） |
| 7 | ノイズ: `flat_noise(P) / flat_noise(d_base) ≤ 1.5`【推測: `ADJ_SHARP16_NOISE_MAX` の流用】 | 実効 lift ≥ 40 のプリセット × ISO ≥ 1600 のカット | すべて |
| 8 | 回帰: identity（hr 0）で後段オラクル（ΔE76 中央値 ≤ 0.3、チャンネル差 p99 ≤ 1/255）と既定リクエストの `applied` 差分 0 が保たれる（0.5.0 の出力は保存していないので、この 2 つが実質の回帰確認） | `--adjustments` モード既存チェック | 全合格 |
| 9 | 等価性（§2.2）: `(0, 0, hr 60)` と `(−0.7, 40, hr 0)` の tiff16 が一致 | 1 カット | 一致 |
| 10 | 目視: オーナーが §8 のシートで救済系 3 + 変更の大きいプリセットを確認し合格と判断 | — | 合格 |

### 7.5 反復と打ち切り

1. 初期案（§7.2）で 1 回目を回し、1〜9 の自動判定と ΔE76 表を出す。シートを作る。
2. 不合格 / 目視で違和感のあるプリセットだけ値を直し（1 回に動かすのは 1〜2 キー）、2 回目・3 回目を回す。各回のシートは**変えたプリセットだけ**作り直す。
3. **最大 3 回**。3 回で合わないプリセットは最も近い値で止め、§10 の既知の限界として報告書に書く（本 Issue を止めない）。
4. 結果は `docs/coreimage-preset-retune-report.md`（新規、数値・表・パスのみ。画像は入れない）に残し、比較レポートには §18 として手順と要約だけ足す。

## 8. 目視用の等倍クロップシート

- 置き場所: `work/preset-retune/<run>/`（`work/` は gitignored【事実】）。`sheets/<ID>_<preset>.jpg`、`metrics.csv`、`summary.json`、`tables.md`、`index.md`（見るシートと「見るポイント」の一覧。診断レポート §8 の表と同じ形）。画像はコミットしない。
- 1 枚の構成（`labeled_row` を流用）: 列 = **`d_base`（CI 既定）| 新プリセット（CI）| 同 hr 0（hr > 0 のときだけ）| 旧 rawpy プリセット（参考。「明るさの基準が違う（カメラ比 −0.13）」を注記）**。行 = 全景（長辺 400px）+ `midtone` または `cloud_grad` の等倍 + `fg_dark` の等倍。ROI は `diagnosis_rois`（空のカット）/ `detail_box`（その他）。
- オーナーに見せる組（全部は見ない。オーナー確認 10）:

| 組 | シート | 見るポイント |
|---|---|---|
| 救済系 | DSC04473 / 04607 / 04705 × highlight_rescue / backlit_rescue、原寸 1 枚ずつ | 空の階調が戻るか、前景が沈まないか、境界の偽色 |
| 夕景・薄明 | DSC04532 × sunset_glow | 桃色の雲の階調と暖色の方向・強さ（hr 60 と 70 を並べる） |
| 既定 | DSC04705 / 04473 / 04670 × neutral_recovery（hr 0 と 30） | 案 B / C の選択。CI 既定とどれだけ違うか |
| 風景 | DSC04670 / 04872 × landscape_clear（旧値と弱め候補） | コントラスト・彩度・シャープが強すぎないか（白い粒・硬さ） |
| 室内・高 ISO | DSC04430 / 04432 × indoor_warm / shadow_rescue / nightscape | lift のノイズ、窓の白 |
| 夜景 | DSC07415 / 07407 × nightscape / shadow_rescue | 星の残り、空のノイズ、NR full |

## 9. 変更ファイル一覧とタスク分割

| タスク | 範囲 | 変更ファイル | 完了条件 |
|---|---|---|---|
| **T1 CLI 0.6.0** | 写像・合成・応答・capabilities | `native/coreimage-cli/Sources/CoreImageDevelop/Adjustments.swift`（`HighlightRecoveryMapping`: 節点表・補間・合成・クランプ。`ignoredKeys = []`）、`Develop.swift`（合成の呼び出し、`PostChainParameters.lift` のクランプ、`AdjustmentsApplied.highlight_recovery`）、`Request.swift`（`cliVersion = "0.6.0"`）、`Tests/.../AdjustmentMappingTests.swift`（節点・単調・0 恒等・100・クランプ・§2.2 の等価性。**:236 の `ignored` 期待値を空配列に**）、`SmokeTests.swift:149`（同）、`README.md`（adjustments の表・応答例・`--capabilities` 例・バージョン） | `swift test` 全合格。`--capabilities` の `adjustments.ignored` が `[]` |
| **T2 Rust** | プリセット値・フィクスチャ・モック | `src-tauri/src/presets.rs`（§7.2 の確定値、冒頭コメント「数値は #81 で再調整」、`#[ignore]` の書き出しテスト）、`src-tauri/tests/fixtures/presets.json`（CI 値）、`src-tauri/src/coreimage.rs`（モック `:2856–2872` / `:2952` / `:3251` の `ignored` 期待値。**`:3251` は実バイナリに対するテスト**なので CLI を先に 0.6.0 にする）、`adjustments.rs`（コメントのみ）。`CACHE_KEY_SCHEMA` は上げない | `cargo test` 全合格 |
| **T3 ハーネス** | `--presets` モードとシート | `scripts/compare_engines.py`（`--presets`、`presets.json` の読み込み、`<preset>_hr0`、§7.4 の判定、§8 のシート、§2.2 の等価性チェック。`or_preset` の読み元を `presets.json` に。**オラクル系バリアント（`oracle_adjustments`）は `highlight_recovery` を 0 に固定する**: 今はプリセットの hr をそのまま写しており（no-op だから無害）、0.6.0 ではデコード段と lift に効いて Python 側の `post_chain_oracle` と合わなくなる）、`docs/coreimage-preset-retune-report.md`（新規） | 18 カットで実行、エラー 0、`index.md` が出る |
| **T4 Python** | legacy フィクスチャ | `scripts/export_presets_fixture.py`（出力先と docstring）、`tests/fixtures/presets_legacy_rawpy.json`（新規）、`tests/test_presets_fixture.py`（legacy を読む + 構造一致テスト） | `npm run test:raw` 全合格 |
| **T5 フロント** | 注記・hint | `src/components/BeforeAfter.tsx:155–159`（注記を削除）、`src/lib/format.ts:143`（hint を「露出を下げて暗部を戻す」等に）、`src/lib/types.ts:62`（コメントを CI の写像に）。任意: `coreimage-api.ts: toDevelopedResult` に実効値を通して recipe 欄に出す | `npx tsc --noEmit`・`npm test`・`npm run lint` 合格 |
| **T6 Docs** | 契約・対応表・ADR | `docs/API_CONTRACT.md` §2（hr のコメント）・§5（表を CI 値に、legacy の注記、フィクスチャ分割の説明）、`docs/coreimage-parameter-mapping.md`（「#73 での決定」の hr 行を「#81 での決定」に）、`docs/design/coreimage-preview-adjustments.md` §3.3（hr 行）、`docs/adr/0002-coreimage-engine-decision.md`（追補: #81 完了、ハイライトの条件「満たしている」へ）、`docs/coreimage-regression-check.md` §3.4（`--presets` と等価性チェックを追加）、`native/coreimage-cli/README.md`（T1） | レビューで整合 |

順序: T1 → T3（ハーネスのコードは T1 と並行可。実行は T1 の後）→ T3 の反復（§7.5、オーナーの目視）→ T2 の確定値 → T4 / T5 / T6。
T1 と T3 は worktree を分けて並行、T2 は値が確定してから。CLI は `scripts/build-coreimage-cli.sh` で `src-tauri/binaries/` に再生成する（gitignored）。

## 10. リスク

| # | リスク | 説明 | 手当て |
|---|---|---|---|
| 1 | 全プリセットが暗くなる（案 A） | 旧 hr を残すと 11 プリセットが −0.64〜−1.3EV 寄りになり、承認済みの `d_base` の画から外れる | 案 B（§4）。オーナー確認 1 |
| 2 | 空の無いシーンで C1' が未検証 | #92 の 10 カットは空が主役。室内・人物・夜景で露出低下 + lift の見え方は未計測 | 案 B なら影響は救済系 3 に限られる。§7.3 の代表カットで測る |
| 3 | 夕景の代表カットが無い | `sunset_glow` の hr 60 は夜明けの 1 カット（DSC04532）と空のカットで決める。太陽そのものは未検証 | 報告書に既知の限界として書く。追加撮影はしない（#92 の判断） |
| 4 | 後段の効きが rawpy 時代より強く見える | `apple_default` は boost トーン・彩度高め・デコーダ内シャープが入っており、同じ 1.10 / 1.08 / 1.15 が強く出る【推測】 | §7.2 の弱め候補をシートに並べる |
| 5 | lift の上積みで高 ISO のノイズが増える | `backlit_rescue`（lift 70）を ISO 2000 の室内で使うと暗部ノイズが増える【推測】 | §7.4 の 7 で測る。超えるなら lift 30 → 20 |
| 6 | 原寸の `magenta_rate` | preview の閾値 0.01 を原寸は超える（C1' で 0.0235）。#92 の申し送り | 原寸は目視のみ。閾値の見直しは別 Issue |
| 7 | プレビューと書き出しの値の差が広がる | プレビュー = CI + 新値、書き出し = rawpy + 旧値。ADR 0002 追補で「見え方の不一致」は受容済みだが、**プリセット値まで違う**ことは未説明。さらに overrides は「触ったキーの絶対値」で両方の表に重なるので、基準値が違うキー（例: `highlight_rescue` の `shadow_lift` CI 0 / rawpy 15）では**同じスライダー操作でプレビューと書き出しが逆方向に動く**（5 にすると CI は上がり rawpy は下がる） | オーナー確認 6。rawpy 取りやめ（#90）で解消 |
| 8 | フィクスチャ分割で Python ↔ Rust の数値一致を放棄 | 構造（名前・キー・値域）の一致だけ残す | §6 の構造一致テスト。数値の一致は移行期間中は不要（意図して違う） |
| 9 | スライダー表示と実効値のずれ | `shadow_lift 15` と表示されるが画は lift 55 | オーナー確認 8（実効値の表示は小改修） |
| 10 | hr 100 の外挿 | (−1.1, 60) は未計測 | §7.3 で `highlight_rescue` を hr 100 でも回す |
| 11 | OS 更新で `apple_default` のトーンが変わると写像の節点がずれる | 節点は 2026-10-21 の macOS 27.0.1 での計測値 | `coreimage-regression-check.md` §3.4 に `--presets` を足し、等価性と clip8 の減りを確認する |

## 11. オーナー確認事項

各項目は推奨を書いてある。説明無しに「はい / いいえ」を求めない。

1. **既定 hr の見直し（案 B）**: 非救済 8 プリセットの hr を 0 にし、hr > 0 は救済系 3 だけにする。理由: ADR 0002 で承認した画は `d_base` そのもので、C1' は空のカットでの救済の見え方として承認したもの。残すと全プリセットが −0.64EV 以上暗くなる。**推奨: 採用**。
2. **完了条件の改訂**: 「救済系 3 プリセットで白飛び率が CI 既定より下がり、中間調が CI 既定 ±0.03 以内。他のプリセットは宣言した方向の差を確認」（§7.4）。Issue 本文の rawpy 比の条件は無効にする。**推奨: 採用**。
3. **写像の形と合成**: 区分線形（0 恒等、60 / 90 は承認値、100 は外挿）+ 加算合成。max / 置換はスライダーに不感帯を作る。**推奨: 採用**。
4. **`highlight_rescue` の値**: 旧 `exposure −0.3 / shadow_lift 15` を 0 / 0 にして hr 90 だけで C1' 強め（−1.0, 55）に一致させる。旧値のままだと −1.3EV / lift 70 で未計測の領域。**推奨: 採用**（hr 100 の (−1.1, 60) も並べて見せる）。
5. **`neutral_recovery` / `landscape_clear` に軽い hr 30 を入れるか（案 C）**: 空の白飛び 13.0% を半分程度に減らせる見込みだが、既定の画が CI 既定から外れる。**シート（§8「既定」の組）を見て決める。推奨: まず hr 0（案 B）で出し、C は選択肢として並べる**。
6. **プレビューと書き出しの差の受容**: 移行完了まで、プレビュー（CI・新値）と書き出し（rawpy・旧値）はプリセット値も違う。**推奨: 受容**（#90 で解消）。
7. **フィクスチャの分割**: Python と Rust の数値一致テストをやめ、構造の一致だけ残す。**推奨: 採用**。
8. **実効値の表示**: 比較欄の recipe に「ハイライト回復 90（露出 −1.0 EV / 暗部 +55 として適用）」を出すか。`toDevelopedResult` の小改修。**推奨: 入れる（T5 の任意項目）。不要ならスライダーの hint だけ直す**。
9. **CLI 0.6.0 と `ignored` の空配列**: 契約上の小変更（フィールドは残す）。`CACHE_KEY_SCHEMA` は上げない。**推奨: 採用**。
10. **目視の量**: 18 カット × 11 プリセットの全シートは見ない。§8 の 6 組（約 20 枚）に絞る。**推奨: 採用**。
11. **原寸の偽色の閾値**: 原寸は目視のみとし、閾値 0.01 の見直しは別 Issue にする。**推奨: 採用**。
12. **`sunset_glow` の hr**: 夕景の代表カットが無いまま 60 で決める（夜明け 1 カットで確認）。**推奨: 60 で出し、70 をシートに並べる**。

### オーナー判断（2026-10-22 確定）

| # | 項目 | 判断 |
|---|---|---|
| 1 | 既定 hr | **案 B を採用**。非救済 8 プリセットの hr を 0、hr > 0 は `highlight_rescue` / `backlit_rescue` / `sunset_glow` だけ。案 C（`neutral_recovery` / `landscape_clear` に hr 30）は値を 0 のまま実装し、比較シートで hr 0 と並べて提示する |
| 3 | 写像の形と合成 | **区分線形 + 加算を採用**。救済系プリセット側の `exposure` / `shadow_lift` を §7.2 の初期案どおり整理する（`highlight_rescue` = exposure 0 / lift 0 / hr 90、`sunset_glow` = exposure 0 / hr 60、`backlit_rescue` = lift 30 / hr 60） |
| 6 | プレビューと書き出しの差 | **受容**。フィクスチャは §6 のとおり 2 本に分割する |
| 2・4・5・7〜12 | その他 | 推奨どおり進める: 完了条件は §7.4 に改訂、`highlight_rescue` は 0 / 0 / hr 90（hr 100 もシートに並べる）、フィクスチャ分割、実効値の UI 表示を入れる（T5）、CLI 0.6.0・`ignored` 空配列・`CACHE_KEY_SCHEMA` 不変、目視は §8 の 6 組、原寸の偽色は目視のみ、`sunset_glow` は hr 60 で出し 70 を並べる |

実装時の補足（2026-10-22）: 計測（run1〜run3）で `sunset_glow` の `shadow_lift` を 8 → 4 に改めた（§7.2）。他は §7.2 の初期案のまま確定。
結果は [再調整レポート](../coreimage-preset-retune-report.md)。

### 目視（§8）のオーナー判断（2026-10-22 確定）

- 夜景の組（DSC07415 nightscape / DSC07407 shadow_rescue）: 新プリセット（CI）の列が好ましい。
- 救済系（highlight / backlit / sunset）・室内高 ISO（shadow_rescue / indoor_warm / nightscape）: 新プリセットの値で確定。
  `highlight_rescue` は hr 90（100 にしない）、`sunset_glow` は hr 60（70 にしない）。
- 既定の組（案 C）: **採らない**。`neutral_recovery` / `landscape_clear` は hr 0 のまま。空の白飛びが気になる写真は救済系プリセットで扱う。
- `landscape_clear` の強さ: **今の値のまま**（contrast 1.10 / saturation 1.08 / sharpness 1.15）。弱めた版（1.06 / 1.05 / 1.08）は平坦部のノイズ指標が下がるが、全体表示で差がほぼ見えない。拡大時の粒が問題になったら別 Issue で見直す。
