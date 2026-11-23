# Bitaxe-Gamma-Hex-Benchmark-Tool
Automated voltage and frequency benchmarking utility for the 12V 6-chip Bitaxe Gamma Hex (6× BM1370) running ESP-Miner / AxeOS.

## Modifications for the 12V Bitaxe Gamma Hex

This tool adapts the standard single-chip Bitaxe benchmark script for the **12V 6-chip Bitaxe Gamma Hex (6× BM1370)** with the following hardware guardrails and tuning changes:

- **12V Input Voltage Guardrails (`11.3 V–12.6 V`):** Replaces the single-chip `4.8 V–5.5 V` (`4800–5500 mV`) barrel-plug window so 12V DC operation does not immediately abort on Sample 1.
- **Higher Board Power Cap (`285 W` Default):** Raises the `40 W` single-chip limit to support 6× BM1370 ASICs (~140 W stock, 200+ W overclocked) while protecting the onboard 6-pin C6P / PCIe power connector and PCB traces on a dedicated 12V PSU (such as a Mean Well LRS-350-12).
- **Dual ASIC & VRM Thermal Protection:** Monitors both `temp` and `temp2` (default `66 °C` cutoff) alongside `vrTemp` (default `82 °C` cutoff) so neither ASIC bank nor the VRM stage overheats during 10-minute steps.
- **ASIC Error Ceiling & Valid Share Optimization:** Adds an `errorPercentage` threshold (`0.75%` in `efficiency` mode, `1.50%` in `hashrate` mode) with an early-abort trigger if errors spike past `2.5×` the limit, and ranks high-hashrate runs by **Effective Valid Hashrate** (`averageHashRate × (1 - errorPercentage / 100)`) to maximize valid shares per second.
- **6-Chip Voltage Ceiling (`1300 mV`) & Single-Reboot Exit:** Lowers the maximum core voltage cap from `1400 mV` to `1300 mV` to prevent VRM overload, applies the winning profile for the selected mode (`efficiency` or `hashrate`) on completion or `Ctrl+C`, and removes a redundant double-reboot call on exit.
