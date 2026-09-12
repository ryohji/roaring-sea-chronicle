# 潮鳴り三代記 — リポジトリ憲法

このファイルは全エージェント・全セッションが毎回読む前提の文書である。
ここに書かれた規則は、個別の作業指示より優先される。

## 0. プロジェクト

ファミリーコンピュータ（NES）向けベルトスクロール・アクション。
擬似奥行き4レーン、自律仲間2名、全12話・三世代・2分岐のキャンペーン。

- 企画書（凍結）: `docs/design-doc.md`
- 開発計画: `docs/plan.md`
- 仕様判断の記録: `docs/decisions/`（ADR）
- ROM予算の現況: `docs/budget.md`（`make budget` が自動更新）

## 1. 七箇条

1. **物語を6502コードに書かない。**
   シナリオ・ロスター・台詞・プロップ・ギミック配置は必ず `data/` のテーブル経由。
   `.s` ファイルに固有名詞・台詞・話番号の分岐が現れたら、それは設計の失敗である。

2. **企画書は凍結文書。**
   実装都合で仕様を変えたくなったら、コードを変える前に `docs/decisions/` にADRを起こす。
   ADRなしに `docs/design-doc.md` を編集してはならない（編集権限は spec-keeper のみ）。

3. **キャラクターの個性はグルド家以外では揺らさない。**
   ルート差分の台詞・性格づけを他の3家系（ナギ／トラガ／シオミ）に追加してはならない。

4. **ROM予算は制約ではなく設計対象。**
   `make budget` が予算超過を検出したら、機能追加より削減を優先する。超過したままマージしない。

5. **レーン数は4に固定。**
   擬似奥行きの仕様変更はプロジェクト全体に波及する。ADRなしに触らない。

6. **コミットは1論点1コミット。** ROMバイナリ・ビルド生成物はコミットしない。

7. **実装したら必ず `make test` を通す。** 落ちたまま次の作業に進まない。

## 2. 作業の進め方

- フェーズ（P0〜P8、`docs/plan.md` §5）は**受入条件を満たすまで次に進まない**。
- 同時に走らせるエージェントは**最大2体**。並行可否は `docs/plan.md` §6 に従う。
  - 並行禁止: engine-dev × action-dev、stage-author × engine-dev（P3以前）。
- 各フェーズの末尾で spec-keeper に整合確認をさせる。

## 3. 担当境界（エージェント定義は `.claude/agents/`）

| エージェント | 書込可能範囲 |
|---|---|
| spec-keeper | `docs/` のみ（`src/` は読取のみ） |
| engine-dev | `src/engine/`, `src/main.s`, `src/init.s`, `src/nmi.s`, `src/header.s`, `src/*.inc`, `cfg/` |
| action-dev | `src/action/` |
| ai-dev | `src/ai/`, `data/ai_params.tsv` |
| campaign-dev | `src/campaign/`, `data/flags.tsv`, `data/scenario.tsv`, `data/roster.tsv` |
| stage-author | `src/stage/`, `data/layouts|props|gimmicks|waves/`, `tools/` |
| text-budget | `src/text/`, `data/text/`, `docs/budget.md`, `tools/budget.py` |
| qa-runner | `test/` |

自分の範囲外のファイルを変更する必要が生じたら、**変更せずに報告する**。
境界をまたぐ変更は主（人間）が調停する。

## 4. コーディング規約（ca65）

- ラベルは `snake_case`、定数・マクロは `UPPER_SNAKE`、ローカルラベルは `@loop` 形式。
- ゼロページ変数は `src/zeropage.inc` に一元宣言する。各モジュールで勝手に確保しない。
- モジュール間の呼び出しは必ず `.export` / `.import`。`.global` の乱用禁止。
- 定数・構造体オフセットは `src/constants.inc` に集約し、マジックナンバーを書かない。
- バンク切替を伴う呼び出しは `src/engine/bank.s` のトランポリン経由。直接 MMC3 レジスタを叩かない。
- NMI 中に行ってよいのは OAM DMA・VRAM 転送・スクロール設定・IRQ設定のみ。ゲームロジックを書かない。

## 5. ビルドとテスト

```
make          # ROM を build/roaring.nes にビルド
make test     # 回帰テスト（ROM構造検証 + 6502実行テスト + Mesen Lua があれば実行）
make budget   # PRG/CHR 予算を計測し docs/budget.md を更新。超過なら非0終了
make clean
```

必要ツール: cc65 (ca65/ld65)、Python 3.8+。Mesen2 は任意（`MESEN` 環境変数で指定）。
Mesen2 が無い環境では、`test/harness/` の Python 6502 エミュレータで代替検証する。

## 6. 未決事項

主（人間）の決定待ちの論点は `docs/decisions/` に `Status: Proposed` のADRとして置いてある。
**Proposed のADRに依存する実装を先に進めてはならない。** 迷ったら spec-keeper に確認する。
