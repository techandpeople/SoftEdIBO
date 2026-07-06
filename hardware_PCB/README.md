# Hardware (PCB) files

KiCad design files for the SoftEdIBO node boards:

- **Top-level KiCad project** (the `.kicad_pro` / `.kicad_sch` / `.kicad_pcb`
  files in this directory) — the **multiplexed board** (`node_multiplexed`):
  ESP32-WROOM-32, 74HC4067 sensor mux, PCA9685-driven valves (ULN2803 drivers),
  up to 6 pumps, XGZP6847A pressure sensors.
- **Nested KiCad project** (the subdirectory with its own `.kicad_pro`) — the
  **direct board** (`node_direct`): ESP32-WROOM-32, 3 chambers on direct ADC
  sensors, ULN2803-driven valves, 2 onboard pumps.
- `Data_sheets/` — component datasheets (pump/valve drivers, XGZP6847A pressure
  sensor).
- `libraries/` — shared KiCad symbol/footprint libraries;
  `logic_I2C.kicad_sch` — shared I2C logic sheet.

> The project files carry legacy names on disk; in code and docs the boards are
> always referred to by their functional names — the **direct board**
> (`node_direct`, firmware env `direct`) and the **multiplexed board**
> (`node_multiplexed`, firmware env `multiplexed`). See
> [firmware/node_actuator/](../firmware/node_actuator/).
