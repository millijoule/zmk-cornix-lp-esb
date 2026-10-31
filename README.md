# Cornix LP — ZMK over ESB

Experimental ESB-only split transport build for Cornix LP with a nice!nano USB dongle.

## Topology

```text
Cornix LP left (cornix_ph_left, ESB ID 0) ─┐
                                          ├─ 2.4 GHz ESB ─ nice!nano dongle ─ USB HID ─ PC
Cornix LP right (cornix_right, ESB ID 1) ─┘
```

The dongle is the ZMK split central. Both Cornix halves are ESB peripherals. The
firmware is intentionally configured without BLE and without battery reporting
for the first hardware-validation stage. Display, ZMK Studio, and battery
indicators are also omitted to keep the initial build small.

## Firmware dependencies

The build pins the ZMK and ESB transport revisions and uses the Cornix board
module's v3.0.0 release:

- ZMK: `4dd95d7840eb3d5a3f59bb29fbf1ab054715ef85`
- ESB transport: `badjeff/zmk-feature-split-esb` at
  `314c7cbaf4a74e1add1d6ffc8249de3e29965b8c`
- Nordic SDK: `badjeff/sdk-nrf` at
  `9b3d2623fdcd9c0fd0284f860beea924568c9826`
- nrfxlib: `nrfconnect/sdk-nrfxlib` at
  `dfadf17305d8f000eda9aa74a5b9ff1c5647a23e`
- Cornix board definitions: `hitsmaxft/zmk-keyboard-cornix` v3.0.0

## Initial test scope

1. Build the nice!nano dongle, Cornix LP left peripheral, and right peripheral.
2. Confirm that the key matrix and split event path work with BLE and battery
   reporting disabled.
3. Validate the ESB packet cadence and event delivery under normal typing.
4. Separately measure the USB HID report interval observed by the host.

A 1 kHz ESB transmit cadence and a 1 kHz USB HID polling/report interval are
separate properties. A successful build alone does not prove either one. The
1 kHz target remains unverified until measured on hardware.

## Build

A push to `main`, a pull request, or a manual workflow dispatch runs the ZMK
GitHub Actions build. Download the `firmware` artifact from the latest run.

Expected firmware files:

- `cornix_dongle_esb_nosd.uf2` — nice!nano dongle
- `cornix_left_esb.uf2` — Cornix LP left half
- `cornix_right_esb.uf2` — Cornix LP right half

The Cornix board module provides the board definitions and hardware pin map.
This repository adds the ESB link shield and the role-specific transport
configuration.

## Re-enabling features later

Do not enable BLE or battery reporting until the ESB-only topology is validated.
Those features are intentionally disabled in this first stage and should be
added back in separate, testable changes.
