# data — データテーブル

**物語を6502コードに書かない**（CLAUDE.md 第1条）。
シナリオ・ロスター・台詞・プロップ・ギミック配置は、すべてここのテーブルを正とし、
`tools/` の変換器でバイナリにしてから ROM に載せる。

| ファイル | 内容 | 担当 | 着手 |
|---|---|---|---|
| `scenario.tsv` | 話ID、タイルセットID、レイアウトID、プロップセットID、ギミックID、敵ウェーブ、プール定義、**オートセーブポイント列**（ADR-0001） | campaign-dev | P4 |
| `roster.tsv` | キャラID、家系、体格型、AI型、パレット、固有必殺、世代、加入条件フラグ | campaign-dev | P2 |
| `flags.tsv` | フラグID定義（分岐、処遇、恒久離脱、縁値、研究配分） | campaign-dev | P4 |
| `ai_params.tsv` | AI 6型のパラメータ表 | ai-dev | P2 |
| `layouts/` | ステージのジオメトリ | stage-author | P3 |
| `props/` | レイアウトID × 時代 → 物体配置リスト（1時代 200〜400 B） | stage-author | P3 |
| `gimmicks/` | ルート固有機構（**1ルート1機構に厳守**） | stage-author | P3 |
| `waves/` | 敵ウェーブ | stage-author | P3 |
| `text/` | 話者タグ付き台詞。辞書圧縮の入力 | text-budget | P7 |

変換器は入力を検証し、壊れたデータは**ビルドを失敗させる**こと。黙って通さない。
