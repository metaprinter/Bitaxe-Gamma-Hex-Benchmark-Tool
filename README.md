# Bitaxe-Gamma-Hex-Benchmark-Tool
Automated voltage and frequency benchmarking utility for the 12V 6-chip Bitaxe Gamma Hex (6× BM1370) running ESP-Miner / AxeOS.

## Modifications for the 12V Bitaxe Gamma Hex

This tool adapts the standard single-chip Bitaxe benchmark script for the **12V 6-chip Bitaxe Gamma Hex (6× BM1370)** with the following hardware guardrails and tuning changes:

- **12V Input Voltage Guardrails (`11.3 V–12.6 V`):** Replaces the single-chip `4.8 V–5.5 V` (`4800–5500 mV`) barrel-plug window so 12V DC operation does not immediately abort on Sample 1.
- **Higher Board Power Cap (`285 W` Default):** Raises the `40 W` single-chip limit to support 6× BM1370 ASICs (~140 W stock, 200+ W overclocked) while protecting the onboard 6-pin C6P / PCIe power connector and PCB traces on a dedicated 12V PSU (such as a Mean Well LRS-350-12).
- **Dual ASIC & VRM Thermal Protection:** Monitors both `temp` and `temp2` (default `66 °C` cutoff) alongside `vrTemp` (default `82 °C` cutoff) so neither ASIC bank nor the VRM stage overheats during 10-minute steps.
- **ASIC Error Ceiling & Valid Share Optimization:** Adds an `errorPercentage` threshold (`0.75%` in `efficiency` mode, `1.50%` in `hashrate` mode) with an early-abort trigger if errors spike past `2.5×` the limit, and ranks high-hashrate runs by **Effective Valid Hashrate** (`averageHashRate × (1 - errorPercentage / 100)`) to maximize valid shares per second.
- **6-Chip Voltage Ceiling (`1300 mV`) & Single-Reboot Exit:** Lowers the maximum core voltage cap from `1400 mV` to `1300 mV` to prevent VRM overload, applies the winning profile for the selected mode (`efficiency` or `hashrate`) on completion or `Ctrl+C`, and removes a redundant double-reboot call on exit.

---

## Requirements & Installation

- **Hardware:** Bitaxe Gamma Hex (6× BM1370) running ESP-Miner / AxeOS on the same local network, powered by an adequately rated 12V DC PSU.
- **Software:** Python 3.7+ and the `requests` library.

```bash
git clone https://github.com/metaprinter/Bitaxe-Gamma-Hex-Benchmark-Tool.git
cd Bitaxe-Gamma-Hex-Benchmark-Tool
pip3 install requests
```

---

## Usage

Replace `<BITAXE_IP>` with your miner's local IP address (visible on the Hex LCD screen or your router).

### 1. Full Sweep / Efficiency Mode (`-m efficiency`)
Starts at a conservative undervolt (`1080 mV` / `675 MHz`) with a strict `0.75%` error ceiling and climbs until reaching your thermal, power, or voltage limits:

```bash
python3 bitaxe_hex_benchmark.py <BITAXE_IP> -m efficiency
```

> **Tip:** Because `-m efficiency` starts low and continues climbing all the way to your hardware ceiling, a single full run maps **both** your lowest-`J/TH` sweet spot and your peak valid-hashrate limit in one pass, saving both leaderboards to the JSON output file.

### 2. High Hashrate Mode (`-m hashrate`)
Starts closer to overclocked territory (`1180 mV` / `825 MHz`) with a `1.50%` error ceiling to focus on maximizing valid shares per second:

```bash
python3 bitaxe_hex_benchmark.py <BITAXE_IP> -m hashrate
```

### 3. Custom Starting Coordinates & Limits
Override starting voltage/frequency or safety ceilings on the command line without editing the code:

```bash
# Quick high-clock sweep starting at 1200 mV / 875 MHz
python3 bitaxe_hex_benchmark.py 192.168.1.247 -m hashrate -v 1200 -f 875

# Tighter voltage steps (10 mV) and custom 260 W power cap
python3 bitaxe_hex_benchmark.py 192.168.1.247 -m efficiency --volt-step 10 --max-power 260
```

---

## Command-Line Options

| Flag | Default | Description |
| :--- | :--- | :--- |
| `bitaxe_ip` | *(Required)* | Local IP address of the Bitaxe Gamma Hex (e.g., `192.168.1.247`). |
| `-m`, `--mode` | `efficiency` | Optimization goal: `efficiency` (lowest J/TH) or `hashrate` (highest valid TH/s). |
| `-v`, `--voltage` | `1080` (`efficiency`)<br>`1180` (`hashrate`) | Starting core voltage in mV (allowed range: `1000–1300` mV). |
| `-f`, `--frequency` | `675` (`efficiency`)<br>`825` (`hashrate`) | Starting core frequency in MHz (allowed range: `400–1100` MHz). |
| `--volt-step` | `15` | Core voltage step in mV added when a clock tier fails stability or error limits. |
| `--freq-step` | `25` | Core frequency step in MHz added after each passing benchmark step. |
| `--max-power` | `285.0` | Hard power cutoff in Watts. |
| `--max-temp` | `66.0` | Hard ASIC temperature cutoff in °C enforced across both `temp` and `temp2`. |
| `--max-vr-temp` | `82.0` | Hard voltage regulator (`vrTemp`) cutoff in °C. |
| `--max-error` | `0.75` (`efficiency`)<br>`1.50` (`hashrate`) | Maximum allowed average ASIC `errorPercentage` (`%`). |
| `--benchmark-time` | `600` | Duration of each test tier in seconds (`600` = 10 minutes; minimum `120`s). |

---

## How the Benchmark Works

1. **Device Detection:** Queries `/api/system/info` (with `/api/system/asic` fallback) to confirm chip count (`6` × BM1370 = `12,240` total cores) and baseline settings.
2. **Stabilization & Sampling:** After applying each voltage/frequency pair (`PATCH /api/system`) and rebooting (`POST /api/system/restart`), the tool waits **90 seconds** for PLLs, liquid cooling, and hashrate to settle, then polls telemetry every **15 seconds** for `--benchmark-time` (40 samples by default).
3. **Outlier & Warmup Trimming:** Drops the 3 highest and 3 lowest hashrate readings to filter Stratum variance, and discards the first 6 warmup samples (`90` seconds) when averaging temperatures, power draw, and error percentage.
4. **Adaptive Stepping:**
   - **Pass:** If trimmed hashrate is at least `94%` of theoretical expected hashrate (`frequency × total_cores / 1000`) and average `errorPercentage <= --max-error`, frequency increases by `--freq-step`.
   - **Fail / Error Spike:** If hashrate falls short or error rate exceeds `--max-error` (or spikes past `2.5×` `--max-error` mid-run for early abort), the script steps frequency back by `--freq-step` and raises voltage by `--volt-step`.
5. **Results & Safe Exit:** Saves incremental and final results to `bitaxe_hex_benchmark_<mode>_<ip>_<timestamp>.json` containing `all_results`, `top_valid_hashrate`, and `most_efficient`. Pressing `Ctrl+C` at any time cleanly aborts the sweep, saves completed steps, and reboots the miner into the best profile found for the active mode.

---

## Safety & Tuning Notes

- **Power Supply & Connector Limits:** Keep `--max-power` at or below 90% of your PSU's continuous wattage rating and within the current rating of your 6-pin Molex C6P / PCIe input connector and wiring.
- **VRM Cooling:** Sustained overclocking above 200 W puts heavy load on the rear VRM stages. Active airflow (such as a ducted 60 mm fan) and VRM heatsinks are strongly recommended before raising `--max-power` or `--max-vr-temp`.
- **Ambient Temperature:** Optimal voltage/frequency curves depend on room temperature. Re-run the benchmark if seasonal ambient temperatures rise significantly.
