-- Live-verified Omarchy 4.5.0 Hyprland Lua monitor and touch snippet for
-- ASUS ZenBook Pro Duo UX582HS (Intel iGPU driving both panels; RTX 3080 free
-- for CUDA / ml-os gpu-lease workloads).
--
-- Usage:
--   cp ~/.config/hypr/monitors.lua ~/.config/hypr/monitors.lua.bak.$(date +%s)
--   cp configs/omarchy-4.5-ux582hs-monitors.lua ~/.config/hypr/monitors.lua
--   hyprctl reload && hyprctl configerrors

-- Fallback rule for hot-plugged external monitors.
local omarchy_monitor_scale = "auto"
hl.monitor({ output = "", mode = "preferred", position = "auto", scale = omarchy_monitor_scale })

-- Upper primary 4K OLED (3840x2160 @ scale 2 -> 1920x1080 logical) at 0x0.
hl.monitor({ output = "eDP-1", mode = "3840x2160@60", position = "0x0", scale = 2 })

-- Lower matte IPS ScreenPad Plus (3840x1100 @ scale 2 -> 1920x550 logical)
-- stacked directly below eDP-1 at 0x1080.
hl.monitor({ output = "DP-3", mode = "3840x1100@60", position = "0x1080", scale = 2 })

-- Per-panel ELAN I2C touch and stylus bindings (verified via hyprctl devices -j).
hl.device({ name = "elan9008:00-04f3:2c56", output = "eDP-1" })
hl.device({ name = "elan9008:00-04f3:2c56-stylus", output = "eDP-1" })
hl.device({ name = "elan9009:00-04f3:2c23", output = "DP-3" })
hl.device({ name = "elan9009:00-04f3:2c23-stylus", output = "DP-3" })

-- Whole-number GDK scale matching monitor scale = 2 for XWayland sharpness.
local omarchy_gdk_scale = 2
hl.env("GDK_SCALE", tostring(omarchy_gdk_scale))
