# Bluetooth and cellular bring-up

Status: 2026-10-24. This document separates diagnostics from features that are actually usable in
iOS. **Neither Bluetooth nor cellular service is ready.**

## Current state

| Area | In this tree | Still missing for a user-visible feature |
| --- | --- | --- |
| Wi-Fi | MPSS boot, GLINK/QRTR, host services, WLAN firmware and `en0`/`wifid` integration | WPA3, power save and other hardening |
| SIM diagnostic | Patch 0951: opt-in, read-only QMI DMS/UIM/NAS clients (`wwan=1`) | On-device validation; no subscription or operating-mode changes |
| LTE registration | Not implemented here | AP-side IPA/GSI, then UIM provisioning and NAS registration |
| Mobile data | Not implemented | IPA endpoints/event rings, DPM/WDA/WDS, a network interface and APN configuration |
| iOS cellular UI | `CommCenter` is removed by `tools/sweet-root-tweaks.py` | A CoreTelephony/baseband boundary; only then re-enable CommCenter |
| Bluetooth | No controller driver; `bluetoothd` has no hardware to open | UART/power/firmware/HCI driver and an Apple-compatible Bluetooth boundary |

A running modem is not the same as working cellular service. Likewise, a successful HCI command is
not the same as Bluetooth working in Settings.

## 1. Read-only SIM/network diagnostic (patch 0951)

Build the kernel with patch 0951 and boot with `modem=1 wwan=1`. Add `wifi=1` as well when checking
that the diagnostic does not regress Wi-Fi. `wwan=1` alone does not boot the modem: `modem=1` is
required.

The probe discovers DMS (`0x02`), NAS (`0x03`) and UIM (`0x0b`) over the existing QRTR transport and
sends only these requests every ten seconds:

- DMS `GET_REVISION` (once) and `GET_OPERATING_MODE`;
- UIM `GET_CARD_STATUS`;
- NAS `GET_SERVING_SYSTEM`.

It deliberately does **not** send `SET_OPERATING_MODE`, `CHANGE_PROVISIONING_SESSION`,
`START_NETWORK`, or any EFS write. IMEI, ICCID, AID and raw card responses are not logged.

Useful log lines include:

```text
TopazModem: wwan: read-only QMI probe on (GET only; no provisioning, identifiers hidden)
TopazModem: wwan: uim service up (...)
TopazModem: wwan: SIM slot 2: card present, ...
TopazModem: wwan:   app 0: USIM state ... (...)
TopazModem: wwan: network not registered, ...
```

`not registered` is not, by itself, a failure of this diagnostic. A cold/unprovisioned modem may need
the later IPA and provisioning steps before it can register. This milestone passes when the services
answer, card/application state is decoded without malformed-TLV errors, and modem/Wi-Fi remain up.

The probe is off by default. Do not publish logs containing identifiers from other diagnostic tools;
partition backups and EFS contents remain out of Git.

A host-side parser test needs no firmware or phone. It compiles the exact block embedded in patch
0951, checks synthetic USIM/LTE responses and exercises truncated frames with ASan/UBSan:

```sh
python3 tools/test-wwan-probe.py
# On a compiler without ASan/UBSan:
python3 tools/test-wwan-probe.py --no-sanitize
```

This test does not replace the full XNU build or an on-device modem test.

## 2. Next cellular milestone: IPA/GSI before provisioning

The [topaz-woa SIM log](https://github.com/MakrSas/topaz-woa/blob/main/docs/P9_sim.md) is the most
relevant reference. Its early SIM tests ended in `modem_cfg` starvation because the AP-side IPA was
not ready; its later v0.20 sequence registered a USIM on LTE while Wi-Fi stayed up. Mobile data was
still incomplete there.

Port that sequence in independently testable steps, behind `ipa=1` and without enabling provisioning
until every step succeeds:

1. Reuse `TopazRpm` for the IPA clock vote (100 MHz), once per boot.
2. Set up IPA SMMU streams `0x140`/`0x141`/`0x142` and load `ipa_fws` through PAS 15.
3. Verify GSI firmware is enabled before accessing channels.
4. Use legacy GSI interrupts (`INTSET = 1`) and the required masks. The reference project's MSI
   completion path reset the SoC; this step must not be skipped.
5. Allocate modem GSI channels 0–3 and complete the AP IPA QMI `INIT_DRIVER` /
   `DRIVER_INIT_COMPLETE` exchange.
6. Only then enable UIM provisioning and verify NAS registration **and continued WLAN operation**.

Each step needs a known-good boot image and a separate device log. Do not combine first-time IPA
register access with SIM provisioning in the same test.

## 3. Mobile data and iOS integration

Registration and packet data are separate milestones:

1. Implement AP GSI event rings, TX/RX channels and posted RX buffers.
2. Configure the IPA AP↔modem endpoints, then DPM `OPEN_PORT`, WDA `SET_DATA_FORMAT`, WDS
   `BIND_MUX_DATA_PORT` and `START_NETWORK`.
3. Make APN/IP-family settings configurable; do not copy an operator-specific APN from a reference
   log into the driver.
4. Decode IPv4/DNS/MTU settings and carry packets over a BSD network interface.
5. Design the CoreTelephony boundary and restore CommCenter only when that boundary exists. Calls,
   SMS, PIN unlock and VoLTE are later, separate projects.

## 4. Bluetooth plan

The stock device tree describes a WCN3990 Bluetooth controller on UART with `bt-sw-ctrl` GPIO 87.
There is no finished Bluetooth driver in topaz-woa to port. Use the phone's own DTB and firmware as
the authority and Linux `hci_qca`/`btqca` only as protocol references (not as code to copy into XNU).

Recommended milestones:

1. Identify and verify the exact QUP/UART instance, clocks, pinmux, power/reset sequence and firmware
   files on this handset. Start with non-destructive reads and a `bt=1` gate.
2. Speak HCI H4 at the initial baud rate, then implement Qualcomm patch/NVM download and the operating
   baud-rate switch with correct flow control.
3. Before involving `bluetoothd`, pass controller-level tests: HCI reset, read local version/address,
   LE scan, advertising and a small connection/pairing test. Keep raw addresses out of public logs.
4. Reverse-engineer and implement the IOKit/user-space boundary expected by the iOS 16.7 Bluetooth
   stack. Only then enable `bluetoothd` and Settings integration.
5. Test BLE and pairing first. A2DP/HFP also require an audio transport and are not completed merely
   because the HCI controller works.

## Test discipline

- Change one radio subsystem at a time and retain a known-good Wi-Fi boot image.
- Keep IPA, WWAN provisioning and Bluetooth opt-in until each has passed on-device tests.
- Never write `modemst*`, `fsg`, `persist` or calibration data during bring-up.
- Record the exact kernel/payload hash, boot args, modem firmware version and sanitized log with each
  result. Distinguish compiled, booted, controller-tested and iOS-integrated states.
