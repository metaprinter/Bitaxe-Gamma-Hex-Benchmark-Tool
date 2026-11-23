#!/usr/bin/env python3
"""
Bitaxe Gamma Hex Benchmark & Optimization Tool (Modified 12V Hydro / VRM-Cooled Edition)
Tailored for 6x BM1370 Bitaxe Gamma Hex powered by a dedicated 12V PSU (e.g., Mean Well LRS-350-12)
with dual ASIC temperature sensors (temp, temp2) and active VRM cooling.

Supports two optimization modes:
  - efficiency : Starts at lower voltage/frequency and finds the lowest J/TH sweet spot
                 with minimal ASIC hardware errors.
  - hashrate   : Pushes frequency and voltage within safe power/thermal limits to maximize
                 effective valid hashrate (valid shares per second).
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime
import requests

START_TIME = datetime.now().strftime("%Y-%m-%d_%H-%M")

# ANSI Color Codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Bitaxe Gamma Hex Benchmark Tool (12V / 6-Chip BM1370)"
    )
    parser.add_argument(
        "bitaxe_ip",
        nargs="?",
        help="IP address of the Bitaxe Gamma Hex (e.g., 192.168.1.247)",
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=["efficiency", "hashrate"],
        default="efficiency",
        help="Optimization target: 'efficiency' (lowest J/TH) or 'hashrate' (max valid shares/sec). Default: efficiency",
    )
    parser.add_argument(
        "-v",
        "--voltage",
        type=int,
        default=None,
        help="Initial core voltage in mV (default: 1080 for efficiency, 1180 for hashrate)",
    )
    parser.add_argument(
        "-f",
        "--frequency",
        type=int,
        default=None,
        help="Initial core frequency in MHz (default: 675 for efficiency, 825 for hashrate)",
    )
    parser.add_argument(
        "--volt-step",
        type=int,
        default=15,
        help="Core voltage step increment in mV when stability/error limit is hit (default: 15)",
    )
    parser.add_argument(
        "--freq-step",
        type=int,
        default=25,
        help="Core frequency step increment in MHz (default: 25)",
    )
    parser.add_argument(
        "--max-power",
        type=float,
        default=285.0,
        help="Maximum allowed board power draw in Watts (default: 285W for LRS-350-12 + 6-pin C6P)",
    )
    parser.add_argument(
        "--max-temp",
        type=float,
        default=66.0,
        help="Maximum allowed ASIC temperature in °C across both sensors (default: 66°C)",
    )
    parser.add_argument(
        "--max-vr-temp",
        type=float,
        default=82.0,
        help="Maximum allowed VRM temperature in °C (default: 82°C)",
    )
    parser.add_argument(
        "--max-error",
        type=float,
        default=None,
        help="Maximum allowed ASIC errorPercentage (default: 0.75%% for efficiency, 1.50%% for hashrate)",
    )
    parser.add_argument(
        "--benchmark-time",
        type=int,
        default=600,
        help="Duration of each benchmark step in seconds (default: 600 = 10 min)",
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()
    if not args.bitaxe_ip:
        parser.print_help()
        sys.exit(1)

    return args


args = parse_arguments()
bitaxe_ip = f"http://{args.bitaxe_ip}"
mode = args.mode

# Mode-specific defaults if not explicitly overridden on CLI
initial_voltage = args.voltage if args.voltage is not None else (1080 if mode == "efficiency" else 1180)
initial_frequency = args.frequency if args.frequency is not None else (675 if mode == "efficiency" else 825)
max_error_rate = args.max_error if args.max_error is not None else (0.75 if mode == "efficiency" else 1.50)

# Configuration & Safety Guardrails (Modified Bitaxe Gamma Hex)
voltage_increment = args.volt_step
frequency_increment = args.freq_step
sleep_time = 90                     # Wait 90s after reboot for PLLs, loop, and hashrate to stabilize
benchmark_time = args.benchmark_time
sample_interval = 15                # 15s telemetry polling interval
max_temp = args.max_temp            # Enforced on both temp (ASIC 1) and temp2 (ASIC 2)
max_vr_temp = args.max_vr_temp      # Enforced on vrTemp (protected by ducted 60mm VRM fan)
max_power = args.max_power          # 285W cap protects 6-pin C6P board connector on LRS-350-12

min_allowed_voltage = 1000          # Minimum core voltage (mV)
max_allowed_voltage = 1300          # Maximum safe core voltage (mV) for 6x BM1370
min_allowed_frequency = 400         # Minimum core frequency (MHz)
max_allowed_frequency = 1100        # Maximum core frequency (MHz)
min_input_voltage = 11300           # 11.3V minimum 12V rail threshold (mV)
max_input_voltage = 12600           # 12.6V maximum 12V rail threshold (mV)

# Validation checks
if not (min_allowed_voltage <= initial_voltage <= max_allowed_voltage):
    raise ValueError(
        RED + f"Error: Initial voltage {initial_voltage}mV must be between {min_allowed_voltage}mV and {max_allowed_voltage}mV." + RESET
    )

if not (min_allowed_frequency <= initial_frequency <= max_allowed_frequency):
    raise ValueError(
        RED + f"Error: Initial frequency {initial_frequency}MHz must be between {min_allowed_frequency}MHz and {max_allowed_frequency}MHz." + RESET
    )

if benchmark_time / sample_interval < 8:
    raise ValueError(
        RED + "Error: Benchmark time is too short. At least 8 samples are required for warmup trimming." + RESET
    )

# Global state
small_core_count = None
asic_count = None
results = []
default_voltage = None
default_frequency = None
handling_interrupt = False
system_reset_done = False


def fetch_default_settings():
    global default_voltage, default_frequency, small_core_count, asic_count

    try:
        response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
        response.raise_for_status()
        system_info = response.json()

        if "smallCoreCount" not in system_info:
            print(RED + "Error: smallCoreCount field missing from /api/system/info response." + RESET)
            sys.exit(1)

        small_core_count = system_info.get("smallCoreCount")

        has_voltage = "coreVoltage" in system_info
        has_frequency = "frequency" in system_info
        has_asic_count = "asicCount" in system_info

        if has_voltage and has_frequency and has_asic_count:
            default_voltage = system_info.get("coreVoltage", 1150)
            default_frequency = system_info.get("frequency", 700)
            asic_count = system_info.get("asicCount", 6)
            print(
                GREEN
                + f"Connected to Bitaxe Gamma Hex ({args.bitaxe_ip}):\n"
                + f"  Baseline Core Voltage : {default_voltage} mV\n"
                + f"  Baseline Frequency    : {default_frequency} MHz\n"
                + f"  ASIC Configuration    : {asic_count} chips ({small_core_count * asic_count} total cores)\n"
                + f"  Benchmark Mode        : {mode.upper()} (max error: {max_error_rate:.2f}%, max power: {max_power:.0f}W)"
                + RESET
            )
            return
        else:
            print(
                YELLOW
                + f"Got smallCoreCount ({small_core_count}) from /api/system/info, querying /api/system/asic..."
                + RESET
            )
    except requests.exceptions.RequestException as e:
        print(RED + f"Error fetching from /api/system/info: {e}" + RESET)
        sys.exit(1)

    try:
        response = requests.get(f"{bitaxe_ip}/api/system/asic", timeout=10)
        response.raise_for_status()
        asic_info = response.json()

        default_voltage = asic_info.get("defaultVoltage", 1150)
        default_frequency = asic_info.get("defaultFrequency", 700)
        asic_count = asic_info.get("asicCount", 6)

        print(
            GREEN
            + f"Connected to Bitaxe Gamma Hex ({args.bitaxe_ip}):\n"
            + f"  Baseline Core Voltage : {default_voltage} mV\n"
            + f"  Baseline Frequency    : {default_frequency} MHz\n"
            + f"  ASIC Configuration    : {asic_count} chips ({small_core_count * asic_count} total cores)\n"
            + f"  Benchmark Mode        : {mode.upper()} (max error: {max_error_rate:.2f}%, max power: {max_power:.0f}W)"
            + RESET
        )
    except requests.exceptions.RequestException as e:
        print(RED + f"Error fetching from /api/system/asic: {e}" + RESET)
        sys.exit(1)


def get_system_info():
    retries = 3
    for attempt in range(retries):
        try:
            response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            print(YELLOW + f"Timeout fetching system info (attempt {attempt + 1}/{retries})." + RESET)
        except requests.exceptions.ConnectionError:
            print(RED + f"Connection error fetching system info (attempt {attempt + 1}/{retries})." + RESET)
        except requests.exceptions.RequestException as e:
            print(RED + f"Error fetching system info: {e}" + RESET)
            break
        time.sleep(5)
    return None


def restart_system(wait_for_stabilization=True):
    try:
        if wait_for_stabilization and not handling_interrupt:
            print(YELLOW + f"Rebooting Hex and waiting {sleep_time}s for thermal/hashrate stabilization..." + RESET)
            response = requests.post(f"{bitaxe_ip}/api/system/restart", timeout=10)
            response.raise_for_status()
            time.sleep(sleep_time)
        else:
            print(YELLOW + "Rebooting Hex to apply final settings..." + RESET)
            response = requests.post(f"{bitaxe_ip}/api/system/restart", timeout=10)
            response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(RED + f"Error restarting the system: {e}" + RESET)


def set_system_settings(core_voltage, frequency, wait_for_stabilization=True):
    settings = {
        "coreVoltage": core_voltage,
        "frequency": frequency,
    }
    try:
        response = requests.patch(f"{bitaxe_ip}/api/system", json=settings, timeout=10)
        response.raise_for_status()
        print(YELLOW + f"Applying settings: Core Voltage = {core_voltage}mV, Frequency = {frequency}MHz" + RESET)
        time.sleep(2)
        restart_system(wait_for_stabilization=wait_for_stabilization)
    except requests.exceptions.RequestException as e:
        print(RED + f"Error setting system settings: {e}" + RESET)


def select_best_result():
    if not results:
        return None
    if mode == "efficiency":
        # Lowest J/TH wins in efficiency mode
        return sorted(results, key=lambda x: (x["efficiencyJTH"], -x["effectiveHashRate"]))[0]
    else:
        # Highest valid effective hashrate (most valid shares/sec) wins in hashrate mode
        return sorted(results, key=lambda x: (x["effectiveHashRate"], -x["efficiencyJTH"]), reverse=True)[0]


def reset_to_best_setting():
    best = select_best_result()
    if best is None:
        print(YELLOW + "No valid benchmark runs recorded. Restoring baseline settings." + RESET)
        set_system_settings(default_voltage, default_frequency, wait_for_stabilization=False)
    else:
        print(
            GREEN
            + f"\nApplying optimal {mode.upper()} mode settings:\n"
            + f"  Core Voltage       : {best['coreVoltage']} mV\n"
            + f"  Frequency          : {best['frequency']} MHz\n"
            + f"  Effective Hashrate : {best['effectiveHashRate'] / 1000:.2f} TH/s (Raw: {best['averageHashRate'] / 1000:.2f} TH/s)\n"
            + f"  ASIC Error Rate    : {best['averageErrorPct']:.2f}%\n"
            + f"  Efficiency         : {best['efficiencyJTH']:.2f} J/TH\n"
            + f"  Power Draw         : {best['averagePower']:.1f} W"
            + RESET
        )
        set_system_settings(best["coreVoltage"], best["frequency"], wait_for_stabilization=False)


def build_report_payload():
    top_hashrate = sorted(results, key=lambda x: x["effectiveHashRate"], reverse=True)[:5]
    top_efficiency = sorted(results, key=lambda x: x["efficiencyJTH"], reverse=False)[:5]
    return {
        "device": "Bitaxe Gamma Hex (6x BM1370)",
        "ip": args.bitaxe_ip,
        "mode": mode,
        "timestamp": START_TIME,
        "limits": {
            "maxTempC": max_temp,
            "maxVRTempC": max_vr_temp,
            "maxPowerW": max_power,
            "maxErrorPct": max_error_rate,
            "maxVoltageMv": max_allowed_voltage,
            "maxFrequencyMhz": max_allowed_frequency,
        },
        "all_results": results,
        "top_valid_hashrate": [
            {"rank": i, **r} for i, r in enumerate(top_hashrate, 1)
        ],
        "most_efficient": [
            {"rank": i, **r} for i, r in enumerate(top_efficiency, 1)
        ],
    }


def save_results():
    try:
        ip_address = bitaxe_ip.replace("http://", "")
        filename = f"bitaxe_hex_benchmark_{mode}_{ip_address}_{START_TIME}.json"
        with open(filename, "w") as f:
            json.dump(build_report_payload(), f, indent=4)
        print(GREEN + f"Results saved to {filename}" + RESET)
    except IOError as e:
        print(RED + f"Error saving results to file: {e}" + RESET)


def handle_sigint(signum, frame):
    global system_reset_done, handling_interrupt

    if handling_interrupt or system_reset_done:
        return

    handling_interrupt = True
    print(RED + "\nBenchmarking interrupted by user (Ctrl+C)." + RESET)

    try:
        reset_to_best_setting()
        save_results()
    finally:
        system_reset_done = True
        handling_interrupt = False
        sys.exit(0)


signal.signal(signal.SIGINT, handle_sigint)


def benchmark_iteration(core_voltage, frequency):
    current_time = time.strftime("%H:%M:%S")
    print(
        CYAN
        + f"\n[{current_time}] Benchmarking Core Voltage: {core_voltage}mV | Frequency: {frequency}MHz"
        + RESET
    )

    hash_rates = []
    temperatures_1 = []
    temperatures_2 = []
    power_consumptions = []
    vr_temps = []
    error_rates = []

    start_shares_accepted = None
    end_shares_accepted = None
    start_shares_rejected = None
    end_shares_rejected = None

    total_samples = benchmark_time // sample_interval
    expected_hashrate = frequency * ((small_core_count * asic_count) / 1000.0)

    for sample in range(total_samples):
        info = get_system_info()
        if info is None:
            print(YELLOW + "Skipping step: failed to fetch /api/system/info." + RESET)
            return None, "SYSTEM_INFO_FAILURE"

        temp1 = info.get("temp")
        temp2 = info.get("temp2")
        vr_temp = info.get("vrTemp")
        input_voltage = info.get("voltage")
        hash_rate = info.get("hashRate")
        power_consumption = info.get("power")
        error_pct = info.get("errorPercentage", 0.0)

        shares_acc = info.get("sharesAccepted")
        shares_rej = info.get("sharesRejected")
        if shares_acc is not None:
            if start_shares_accepted is None:
                start_shares_accepted = shares_acc
            end_shares_accepted = shares_acc
        if shares_rej is not None:
            if start_shares_rejected is None:
                start_shares_rejected = shares_rej
            end_shares_rejected = shares_rej

        if temp1 is None:
            print(YELLOW + "ASIC Temperature 1 data unavailable." + RESET)
            return None, "TEMPERATURE_DATA_FAILURE"

        if temp1 < 5:
            print(YELLOW + "Temperature below 5°C; sensor anomaly." + RESET)
            return None, "TEMPERATURE_BELOW_5"

        # Thermal safety checks across both ASIC sensors and VRMs
        if temp1 >= max_temp:
            print(RED + f"ASIC Temp 1 reached {temp1:.1f}°C (limit {max_temp}°C)! Aborting step." + RESET)
            return None, "CHIP_TEMP1_EXCEEDED"

        if temp2 is not None and temp2 >= max_temp:
            print(RED + f"ASIC Temp 2 reached {temp2:.1f}°C (limit {max_temp}°C)! Aborting step." + RESET)
            return None, "CHIP_TEMP2_EXCEEDED"

        if vr_temp is not None and vr_temp >= max_vr_temp:
            print(RED + f"VR Temp reached {vr_temp:.1f}°C (limit {max_vr_temp}°C)! Aborting step." + RESET)
            return None, "VR_TEMP_EXCEEDED"

        # 12V rail checks
        if input_voltage is not None:
            if input_voltage < min_input_voltage:
                print(RED + f"Input voltage dropped to {input_voltage}mV (min {min_input_voltage}mV)! Aborting." + RESET)
                return None, "INPUT_VOLTAGE_BELOW_MIN"
            if input_voltage > max_input_voltage:
                print(RED + f"Input voltage spiked to {input_voltage}mV (max {max_input_voltage}mV)! Aborting." + RESET)
                return None, "INPUT_VOLTAGE_ABOVE_MAX"

        if hash_rate is None or power_consumption is None:
            print(YELLOW + "Hashrate or Power telemetry missing." + RESET)
            return None, "HASHRATE_POWER_DATA_FAILURE"

        if power_consumption > max_power:
            print(RED + f"Power draw reached {power_consumption:.1f}W (limit {max_power:.0f}W)! Aborting step." + RESET)
            return None, "POWER_CONSUMPTION_EXCEEDED"

        # Early abort if error rate explodes well past stability threshold after initial warmup
        if sample >= 6 and error_pct is not None and error_pct > (max_error_rate * 2.5):
            print(
                YELLOW
                + f"ASIC error rate spiked to {error_pct:.2f}% (> {max_error_rate * 2.5:.2f}%). Ending step early."
                + RESET
            )
            return {
                "stable": False,
                "reason": "HIGH_ERROR_RATE_EARLY_ABORT",
            }, None

        hash_rates.append(hash_rate)
        temperatures_1.append(temp1)
        if temp2 is not None and temp2 > 0:
            temperatures_2.append(temp2)
        power_consumptions.append(power_consumption)
        if vr_temp is not None and vr_temp > 0:
            vr_temps.append(vr_temp)
        if error_pct is not None:
            error_rates.append(error_pct)

        pct_done = ((sample + 1) / total_samples) * 100
        inst_eff = power_consumption / (hash_rate / 1000.0) if hash_rate > 0 else 0.0
        t2_str = f" | T2: {temp2:4.1f}°C" if (temp2 is not None and temp2 > 0) else ""
        vr_str = f" | VR: {int(vr_temp):2d}°C" if (vr_temp is not None and vr_temp > 0) else ""
        iv_str = f"{input_voltage / 1000:.2f}V" if input_voltage is not None else "N/A"

        print(
            f"[{sample + 1:2d}/{total_samples:2d}] {pct_done:5.1f}% | "
            f"{core_voltage:4d}mV {frequency:4d}MHz | "
            f"H: {hash_rate / 1000:5.2f} TH/s | "
            f"Err: {error_pct:4.2f}% | "
            f"P: {power_consumption:5.1f}W ({inst_eff:5.2f} J/TH) | "
            f"IV: {iv_str} | T1: {temp1:4.1f}°C{t2_str}{vr_str}"
        )

        if sample < total_samples - 1:
            time.sleep(sample_interval)

    if not (hash_rates and temperatures_1 and power_consumptions):
        return None, "NO_DATA_COLLECTED"

    # Trim 3 lowest and 3 highest hashrate outliers
    sorted_hr = sorted(hash_rates)
    trimmed_hr = sorted_hr[3:-3] if len(sorted_hr) > 6 else sorted_hr
    avg_hashrate = sum(trimmed_hr) / len(trimmed_hr)

    if avg_hashrate <= 0:
        print(RED + "Warning: Zero hashrate detected." + RESET)
        return None, "ZERO_HASHRATE"

    # Trim first 6 warmup readings for temps, power, and error rate
    warmup = 6 if len(temperatures_1) > 6 else 0
    avg_temp1 = sum(sorted(temperatures_1)[warmup:]) / len(sorted(temperatures_1)[warmup:])
    avg_temp2 = (
        sum(sorted(temperatures_2)[warmup:]) / len(sorted(temperatures_2)[warmup:])
        if len(temperatures_2) > warmup
        else None
    )
    avg_vr_temp = (
        sum(sorted(vr_temps)[warmup:]) / len(sorted(vr_temps)[warmup:])
        if len(vr_temps) > warmup
        else None
    )
    avg_power = sum(power_consumptions[warmup:]) / len(power_consumptions[warmup:])
    avg_error_pct = (
        sum(error_rates[warmup:]) / len(error_rates[warmup:])
        if len(error_rates) > warmup
        else 0.0
    )

    # Effective valid hashrate discounts hardware errors (tracks valid shares/sec)
    effective_hashrate = avg_hashrate * max(0.0, (1.0 - (avg_error_pct / 100.0)))
    efficiency_jth = avg_power / (avg_hashrate / 1000.0)
    effective_efficiency_jth = avg_power / (effective_hashrate / 1000.0) if effective_hashrate > 0 else 999.0

    shares_accepted_delta = (
        (end_shares_accepted - start_shares_accepted)
        if (start_shares_accepted is not None and end_shares_accepted is not None)
        else None
    )
    shares_rejected_delta = (
        (end_shares_rejected - start_shares_rejected)
        if (start_shares_rejected is not None and end_shares_rejected is not None)
        else None
    )

    # Stability requires both clean hashrate tracking (>= 94% of expected) and low error rate
    hashrate_ok = (avg_hashrate >= expected_hashrate * 0.94) and (avg_error_pct <= max_error_rate)

    print(
        GREEN
        + f"Summary ({core_voltage}mV @ {frequency}MHz): "
        + f"Raw {avg_hashrate / 1000:.2f} TH/s | Valid {effective_hashrate / 1000:.2f} TH/s | "
        + f"Err {avg_error_pct:.2f}% | {avg_power:.1f}W | {efficiency_jth:.2f} J/TH "
        + f"(Valid: {effective_efficiency_jth:.2f} J/TH)"
        + RESET
    )

    metrics = {
        "coreVoltage": core_voltage,
        "frequency": frequency,
        "averageHashRate": round(avg_hashrate, 2),
        "effectiveHashRate": round(effective_hashrate, 2),
        "expectedHashRate": round(expected_hashrate, 2),
        "averageErrorPct": round(avg_error_pct, 3),
        "averagePower": round(avg_power, 2),
        "efficiencyJTH": round(efficiency_jth, 2),
        "effectiveEfficiencyJTH": round(effective_efficiency_jth, 2),
        "averageTemp1": round(avg_temp1, 2),
    }
    if avg_temp2 is not None:
        metrics["averageTemp2"] = round(avg_temp2, 2)
    if avg_vr_temp is not None:
        metrics["averageVRTemp"] = round(avg_vr_temp, 2)
    if shares_accepted_delta is not None:
        metrics["sharesAcceptedDelta"] = shares_accepted_delta
    if shares_rejected_delta is not None:
        metrics["sharesRejectedDelta"] = shares_rejected_delta

    return {"stable": hashrate_ok, "metrics": metrics}, None


# Main Benchmarking Execution
try:
    fetch_default_settings()

    print(RED + "\nDISCLAIMER:" + RESET)
    print("This tool stress tests your modified Bitaxe Gamma Hex across voltage/frequency tiers.")
    print(f"Hard stops active: ASIC1/2 >= {max_temp}°C | VR >= {max_vr_temp}°C | Power > {max_power:.0f}W | Error > {max_error_rate:.2f}%\n")

    current_voltage = initial_voltage
    current_frequency = initial_frequency
    prev_efficiency = None

    while current_voltage <= max_allowed_voltage and current_frequency <= max_allowed_frequency:
        set_system_settings(current_voltage, current_frequency, wait_for_stabilization=True)
        outcome, abort_reason = benchmark_iteration(current_voltage, current_frequency)

        if outcome is None:
            print(
                GREEN
                + f"Stopped advancing: reached hardware/thermal/power limit ({abort_reason})."
                + RESET
            )
            break

        if outcome["stable"]:
            m = outcome["metrics"]
            results.append(m)
            save_results()

            # In efficiency mode, warn if J/TH is climbing sharply even while stable
            if mode == "efficiency" and prev_efficiency is not None:
                if m["efficiencyJTH"] > prev_efficiency + 1.25:
                    print(
                        YELLOW
                        + f"Efficiency degraded ({prev_efficiency:.2f} -> {m['efficiencyJTH']:.2f} J/TH); "
                        + "continuing scan in case higher clock unlocks a better ratio."
                        + RESET
                    )
            prev_efficiency = m["efficiencyJTH"]

            if current_frequency + frequency_increment <= max_allowed_frequency:
                current_frequency += frequency_increment
            else:
                print(GREEN + "Reached maximum configured frequency with stable results." + RESET)
                break
        else:
            # Unstable hashrate or error rate exceeded max_error_rate: step back freq and add voltage
            if current_voltage + voltage_increment <= max_allowed_voltage:
                current_voltage += voltage_increment
                current_frequency = max(min_allowed_frequency, current_frequency - frequency_increment)
                print(
                    YELLOW
                    + f"Stability/error threshold hit. Stepping back to {current_frequency}MHz and raising voltage to {current_voltage}mV."
                    + RESET
                )
            else:
                print(GREEN + "Reached maximum allowed core voltage. Stopping sweep." + RESET)
                break

except Exception as e:
    print(RED + f"Unexpected error: {e}" + RESET)
finally:
    if not system_reset_done:
        reset_to_best_setting()
        save_results()
        system_reset_done = True

    if results:
        top_valid_hr = sorted(results, key=lambda x: x["effectiveHashRate"], reverse=True)[:5]
        top_eff = sorted(results, key=lambda x: x["efficiencyJTH"], reverse=False)[:5]

        print(GREEN + f"\n================ {mode.upper()} BENCHMARK COMPLETE ================" + RESET)

        print(GREEN + "\nTop 5 Most Efficient Settings (Lowest J/TH):" + RESET)
        for i, r in enumerate(top_eff, 1):
            t2_info = f", T2: {r['averageTemp2']:.1f}°C" if "averageTemp2" in r else ""
            vr_info = f", VR: {r['averageVRTemp']:.1f}°C" if "averageVRTemp" in r else ""
            print(
                f"  #{i}: {r['coreVoltage']}mV @ {r['frequency']}MHz -> "
                f"{r['efficiencyJTH']:.2f} J/TH | Valid: {r['effectiveHashRate'] / 1000:.2f} TH/s | "
                f"Err: {r['averageErrorPct']:.2f}% | {r['averagePower']:.1f}W | "
                f"T1: {r['averageTemp1']:.1f}°C{t2_info}{vr_info}"
            )

        print(GREEN + "\nTop 5 Highest Valid Hashrate Settings (Most Valid Shares/sec):" + RESET)
        for i, r in enumerate(top_valid_hr, 1):
            t2_info = f", T2: {r['averageTemp2']:.1f}°C" if "averageTemp2" in r else ""
            vr_info = f", VR: {r['averageVRTemp']:.1f}°C" if "averageVRTemp" in r else ""
            print(
                f"  #{i}: {r['coreVoltage']}mV @ {r['frequency']}MHz -> "
                f"Valid: {r['effectiveHashRate'] / 1000:.2f} TH/s (Raw: {r['averageHashRate'] / 1000:.2f} TH/s) | "
                f"Err: {r['averageErrorPct']:.2f}% | {r['efficiencyJTH']:.2f} J/TH | "
                f"{r['averagePower']:.1f}W | T1: {r['averageTemp1']:.1f}°C{t2_info}{vr_info}"
            )
    else:
        print(RED + "\nNo valid benchmark steps completed." + RESET)
