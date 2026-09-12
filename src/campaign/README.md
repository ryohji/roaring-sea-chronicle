# src/campaign — キャンペーン基盤

担当: **campaign-dev**（`.claude/agents/campaign-dev.md`）

フラグ、ルート分岐、世代継承（縁値・死亡永続・客将繰り上げ）、研究配分、セーブ。

話ID・フラグID・キャラ名をコードに直書きしない。すべて `data/` のテーブル経由。
**セーブの実装は ADR-0001 が Accepted になるまで着手しない。**

実装開始は **P4**。
