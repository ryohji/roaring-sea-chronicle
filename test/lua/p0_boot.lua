-- P0 起動検証（Mesen2 Lua / L3）
--
--   Mesen --testrunner build/roaring.nes test/lua/p0_boot.lua
--
-- L2（Python エミュレータ）が見られないもの——実機同等のタイミング——を見るのがここの役目である。
-- L2 と同じことを重ねて書かないこと（ADR-0004）。

local FRAMES_TO_RUN = 180
local OAM_SHADOW = 0x0200

local failures = {}

local function check(name, ok, detail)
    if ok then
        emu.log("  PASS  " .. name)
    else
        emu.log("  FAIL  " .. name .. "\n        " .. (detail or ""))
        table.insert(failures, name)
    end
end

local frame = 0

local function onFrame()
    frame = frame + 1

    if frame == 30 then
        -- スプライトが画面内にいる
        local y = emu.read(OAM_SHADOW + 0, emu.memType.nesMemory)
        local x = emu.read(OAM_SHADOW + 3, emu.memType.nesMemory)
        check("スプライト0 が画面内にいる", y > 0 and y < 0xEF and x > 0 and x < 0xF8,
              string.format("y=%d x=%d", y, x))
    end

    if frame >= 31 and frame <= 90 then
        -- 右キーを押し続ける
        emu.setInput(0, { right = true })
    end

    if frame == 91 then
        local x = emu.read(OAM_SHADOW + 3, emu.memType.nesMemory)
        check("右キーで右に動く", x > 120, string.format("x=%d（初期位置 120）", x))
    end

    if frame >= FRAMES_TO_RUN then
        if #failures == 0 then
            emu.log("L3: 全て成功")
            emu.stop(0)
        else
            emu.log("L3: " .. #failures .. " 件失敗")
            emu.stop(1)
        end
    end
end

emu.addEventCallback(onFrame, emu.eventType.endFrame)
emu.log("L3 自動プレイ開始")
