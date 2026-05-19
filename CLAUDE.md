# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
idf.py build                    # Compile
idf.py -p COM3 flash            # Flash to device
idf.py -p COM3 monitor          # Serial monitor (Ctrl-] to exit)
idf.py -p COM3 flash monitor    # Build + flash + monitor in one step
idf.py menuconfig               # Open SDK configuration menu
idf.py set-target esp32         # Set chip target
idf.py fullclean                # Clean all build artifacts
```

## Architecture

8-channel signal synthesis firmware for ESP32 using ESP-IDF v5.5.0 with FreeRTOS.

### Signal Generation Pipeline

```
app_main() → NVS init → build_sine_lut() → spi_init() (GPIO + timer) → xTaskCreatePinnedToCore()

timer_cb() [IRAM, 1 kHz periodic]     rtos_thread() [Core 1, priority 24]
  ├─ Increment phase_acc[0..7]           ├─ Busy-wait polls cur_process flag
  ├─ Wrap at CHN_FREQ[n] threshold       ├─ When cur_process==1: write GPIO pins
  ├─ Index sine_lut[n] → My_Data2[n]    └─ Reset cur_process=0
  └─ Set cur_process=1
```

- **Synchronization**: `cur_process` volatile flag (0=waiting for timer, 1=data ready)
- **Output**: 1-bit square waves on 8 GPIO pins at independent frequencies
- **Phase accumulator**: Each channel wraps at its `CHN_FREQ[n]` value, producing a comparison against `CHN_FREQ[n]/2` to generate high/low

### GPIO Pin Mapping

| Channel | GPIO | Frequency |
|---------|------|-----------|
| 1 | 19 | 45 |
| 2 | 23 | 30 |
| 3 | 18 | 42 |
| 4 | 21 | 33 |
| 5 | 27 | 24 |
| 6 | 13 | 39 |
| 7 | 14 | 27 |
| 8 | 4  | 36 |

## Key Files

- `main/station_example_main.c` — Main application: timer ISR (`timer_cb`), GPIO init (`spi_init`), RTOS output thread (`rtos_thread`), entry point (`app_main`)
- `main/ada4255.h` — DAC register struct (`dac_reg_t`), sine LUT arrays, `WREG` macro, `build_sine_lut()` (currently stub), `SetReg()`
- `main/Kconfig.projbuild` — Project configuration options
- `sdkconfig` — Auto-generated ESP-IDF config (use `idf.py menuconfig` to edit, not manually)

## Environment

- ESP-IDF v5.5.0 (`C:\Users\thlab\esp\v5.5\esp-idf`)
- Target: ESP32 (Xtensa dual-core, 240 MHz)
- Flash: COM3, UART, DIO mode, 40 MHz, 2 MB
- Code comments are in Chinese (中文)
