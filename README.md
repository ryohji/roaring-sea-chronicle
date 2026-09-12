# 潮鳴り三代記（仮題）

ファミリーコンピュータ（NES）向け ベルトスクロール・アクション ＋ キャンペーン／世代交代／ルート分岐。

- 企画書（凍結）: [`docs/design-doc.md`](docs/design-doc.md)
- 開発計画: [`docs/plan.md`](docs/plan.md)
- リポジトリ憲法（全エージェント必読）: [`CLAUDE.md`](CLAUDE.md)
- 仕様判断の記録: [`docs/decisions/`](docs/decisions/)
- ROM 予算の現況: [`docs/budget.md`](docs/budget.md)

## ビルド

```sh
sudo apt-get install cc65     # または https://cc65.github.io/
make                          # -> build/roaring.nes （MMC3 / PRG 256KB / CHR 128KB）
make test                     # 回帰テスト
make budget                   # ROM 予算の計測（超過で非0終了）
make clean
```

必要なもの: cc65 (ca65 / ld65)、Python 3.8+（標準ライブラリのみ）。
Mesen2 は任意。`MESEN=/path/to/Mesen make test` で自動プレイ試験（L3）も走る。

## テストの3層（[ADR-0004](docs/decisions/0004-test-harness.md)）

| 層 | 実行環境 | 見るもの |
|---|---|---|
| L1 構造検証 | Python | iNES ヘッダ、バンク構成、割り込みベクタ |
| L2 実行検証 | Python 6502 エミュレータ（`test/harness/`） | リセットから走らせて RAM/OAM/PPU を検証、クラッシュ検出 |
| L3 自動プレイ | Mesen2 + Lua（`test/lua/`） | 実機同等タイミング、代表20経路の通しプレイ |

L3 は Mesen2 がある環境でのみ走る。`make test` は L3 を実行したか否かを必ず出力する。

## チーム編成

`.claude/agents/` に8体のサブエージェントを定義している。役割を狭く切り、担当ディレクトリを分けている。

| エージェント | 担当 | 主な範囲 |
|---|---|---|
| spec-keeper | 仕様の番人、ADR 起草、整合確認 | `docs/` |
| engine-dev | PPU、スクロール、4レーン、スプライト割当、MMC3 | `src/engine/`, `src/*.s`, `cfg/` |
| action-dev | 操作、攻撃、ヒットストップ | `src/action/` |
| ai-dev | 自律仲間AI 6型 | `src/ai/` |
| campaign-dev | フラグ、分岐、世代継承、研究配分、セーブ | `src/campaign/`, `data/*.tsv` |
| stage-author | レイアウト／プロップ／ギミック、ビルドツール | `src/stage/`, `data/`, `tools/` |
| text-budget | 辞書圧縮、台詞、ROM 予算 | `src/text/`, `data/text/`, `docs/budget.md` |
| qa-runner | 回帰テスト、代表20経路 | `test/` |

同時に走らせるのは最大2体（`docs/plan.md` §6）。
**engine-dev × action-dev** と **stage-author × engine-dev（P3以前）** は並行させない。

## 進捗

| フェーズ | 内容 | 状態 |
|---|---|---|
| P0 | 環境と骨格（最小の動く ROM） | **完了**（`make && make test` が通る） |
| P1 | 垂直スライス（S1 が遊べる） | 未着手 — **主の決定3件が前提** |
| P2 | パーティ3名と自律AI | 未着手 |
| P3 | ステージオーサリング基盤 | 未着手 |
| P4 | キャンペーン基盤 | 未着手 |
| P5 | 研究システム | 未着手 |
| P6 | 全12話の実装 | 未着手 |
| P7 | テキストと予算の締め | 未着手 |
| P8 | 仕上げ | 未着手 |

### P1 に入る前に主が決めること

`docs/decisions/` に選択肢と帰結をまとめてある。決定を書き込むまで、依存する実装は始めない。

1. [ADR-0001 セーブ方式](docs/decisions/0001-save-system.md) — バッテリー / パスワード / 併用
2. [ADR-0002 1画面の敵同時数](docs/decisions/0002-max-onscreen-enemies.md) — 敵4体 / 敵6体+フリッカー / レーン単位制限
3. [ADR-0003 操作キャラの切替可否](docs/decisions/0003-character-switching.md) — 出撃前固定 / 随時切替 / 戦闘不能時のみ交代
