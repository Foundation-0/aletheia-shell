-- Omarchy 4.5.0 Hyprland Lua window rule pinning Aletheia Deck to ScreenPad Plus (DP-3).
-- Under Omarchy per-monitor workspaces, DP-3 owns block 1 (workspaces 11-20), so slot 9 = id 19.
o.window("^(aletheia-deck)$", {
  monitor = "DP-3",
  workspace = "19",
  tile = true,
})
