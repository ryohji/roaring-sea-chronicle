# src/ai — 自律仲間AI

担当: **ai-dev**（`.claude/agents/ai-dev.md`）

6型（突撃 / 牽制 / 回収 / 庇護 / 遠隔 / 万能）の行動ロジック。

**型の個性はパラメータ表で表現し、型ごとに別コードを書き散らさない。**
共通の意思決定ループを1本持ち、差は `data/ai_params.tsv` のパラメータで出す。
型ごとに `.s` が増え始めたら設計を疑うこと。

実装開始は **P1（万能型1体 + 敵1種）**、本格実装は **P2**。

## ファイル

| ファイル | 責務 |
|---|---|
| `data/ai_params.tsv` | **パラメータ表の正本。** 型を足す = ここに行を足す |
| `ai_params.s` | 上の写し（変換器が入るまでの暫定。列ごとに1本の `.byte` 配列） |
| `ai_params.inc` | **型に属さない**調整値（専有距離・分散幅・思考の予算） |
| `ai.inc` | 語彙（プロファイル行の番号・目標ID・フラグ）。数値は置かない |
| `ai.s` | 意思決定ループ**1本**。状況評価 → 目標選択 → 行動 |

型ごとの分岐は `ai.s` に**1つも無い**。敵も同じループ・同じ表で動く
（敵専用の思考を別に書くことは「型ごとに別コードを書き散らす」のと同じ失敗である）。

## 意思決定ループ

```
ai_update  （毎フレーム1回）
  ├ ai_sense        場の状況を1回まとめる（操作キャラが猶予中か = ADR-0006 の口）
  ├ ai_think_slice  1フレームに AI_THINK_PER_FRAME 体だけ考え直す（分散実行）
  │   └ ai_think_for
  │        ├ ai_select_target  重みつき距離（横距離 + レーン差×8）で最寄りを選ぶ
  │        └ ai_plan           目標（待機/復帰/交戦）と立ち位置（側・間合い）を決める
  │             └ ai_spread    同じ標的に同じ側から寄る者どうしをずらす
  └ 自律枠の生存者ぶん ai_act_one（軽い）
       ├ ai_move_to_post   立ち位置へ歩く
       │    ├ ai_clear_player_zone  立ち位置を操作キャラの専有距離の外へ出す
       │    └ ai_avoid_allies       立ち位置を仲間から AI_SPREAD 以上離す
       │                            （押し出しで潰れた分散をここで取り戻す）
       ├ ai_lane_follow    標的のレーンへ寄る
       └ ai_try_attack     間合いに入っていれば act_start_attack（action 側が全部やる）
```

**ダメージ・ヒットストップ・ノックバック・ダウンは `src/action/` の持ち分**であり、
ここには書かない。敵の攻撃も `act_start_attack` → `act_hit_scan` → `act_damage` を通る。

## main.s からの呼び方

`src/main.s` は ai-dev の範囲外なので、下記は主が行う。

1. `.import ai_init, ai_update` を足す。
2. `reset_handler` の `jsr action_init` の直後に `jsr ai_init` を足す
   （エンティティを配置し終えた後であること）。
3. `main_loop` の `jsr action_update` と `jsr lane_update_all` の間に `jsr ai_update` を足す。
4. 配置するエンティティに `ent_ai`（`data/ai_params.tsv` の id 列）を詰める。
   詰め忘れても `ai_init` が立場ごとの既定値で埋めるが、詰めるのが正しい。

途中で湧いた1体（P3 の waves）は、配置直後に `ai_init_entity`（X = 番号）を呼ぶ。

## 外に見せている入口

| 記号 | 用途 |
|---|---|
| `ai_init` | シーン開始時の初期化（エンティティを配置し終えた後） |
| `ai_init_entity` (X) | 途中で湧いた1体ぶんの初期化 |
| `ai_update` | 毎フレーム1回 |
| `ai_sit` | 状況評価の結果（`AI_SIT_LEADER_DOWN`）。**読むのは P2 の蘇生行動** |

## 1フレームの費用（実測）

Python 6502 ハーネスで `ai_update` の入口から出口までを計測した値（NTSC 1フレーム = 29780）。

| 場面 | 中央値 | 最大 |
|---|---:|---:|
| 仲間1＋敵3（P1 の確認シーン）・無操作 | 1112 | 3876 |
| 仲間1＋敵3・右へ歩き続ける | 2444 | 4112 |
| 仲間2＋敵6（満員）・無操作 | 2799 | 5354 |
| 仲間2＋敵6（満員）・右へ歩き続ける | 3960 | 6222 |

満員でも1フレーム全体（`action_update` + `ai_update` + `lane_update_all` +
`cam_update` + `oam_build`）の中央値は 9660 サイクルで、予算の約 1/3 である。

費用を下げる手は、増えた順に:
`AI_THINK_PER_FRAME` を減らす / `AI_RETHINK_PER_FRAME` を減らす /
行動側もフレームで分ける（歩く距離を倍にして1フレームおきに動かす）。
