---
description: ROM をビルドする（make）。失敗したらエラー箇所を特定して報告する。
allowed-tools: Bash(make:*), Read, Grep, Glob
---

`make` を実行して `build/roaring.nes` をビルドせよ。

- 成功したら PRG/CHR のサイズとバンク使用量を1行で報告する。
- 失敗したら、ca65/ld65 のエラーを読み、**どのファイルの何行目が原因か**を特定して報告する。
  リンカの `Range error` は、ほぼ必ずバンク溢れかセグメント配置ミスである。`cfg/mmc3.cfg` を見よ。
