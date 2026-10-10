# Mega Mix+ arcade slider bundle

`bridge/diva_bridge.py` runs on Linux and serves a Divaller, ZhouSensor (HDD mode),
HORI NSW-230 or HORI PS4-161 (arcade mode) to the game; it also works with TLAC's
DivallerBridge.dva. Run `python3 diva_bridge.py --tui` to see the controller live.

Copy the two folders in `mods/` into your Mega Mix+ `mods` folder (DivaModLoader required;
on Linux/Proton/Steam Deck use the launch option `WINEDLLOVERRIDES="dinput8=n,b" %command%`).

* `mods/AnySlider/`    - AnySlider built from current source (MMIO ABI v2.0), preconfigured:
                         io_enabled = true, target_controller_type = 6 (HORI NSW-230).
                         Remove any older AnySlider folder first.
* `mods/BridgeSlider/` - BridgeSlider 1.2 (works with AnySlider ABI v1.0 and v2.0; D-pad and
                         shoulder buttons with diva_bridge.py).

Then start bridge/diva_bridge.py, launch the game, and enable
Customize > Game/Control Config > Arcade Controller Settings > Arcade Controller.

`build-notes/` explains how AnySlider.dll was built and the one header patch used.
