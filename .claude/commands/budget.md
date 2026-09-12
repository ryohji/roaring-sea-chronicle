---
description: PRG/CHR の ROM 予算を計測し docs/budget.md を更新する（make budget）。
allowed-tools: Bash(make:*), Bash(python3:*), Read, Grep, Glob, Edit
---

`make budget` を実行せよ。

- バンク単位で報告する。「全体では余っている」は意味がない（MMC3 はバンク単位で詰む）。
- 超過があれば、機能追加ではなく**削減案**を提示する（CLAUDE.md 第4条）。
- 最大リスクはテキスト量である。テキスト系セグメントの増加傾向を必ず見ること。
