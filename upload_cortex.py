#!/usr/bin/env python3
"""
Cortex Configuration Uploader
Uploads device-specific JSON configuration files to Cortex panels.
Automatically manages Windows Ethernet adapter settings.

** MUST RUN AS ADMINISTRATOR **

Usage:
    python upload_cortex.py <device_name> [device_type]

Example:
    python upload_cortex.py 4E11-G09c-Sec1
    python upload_cortex.py 4C11-R01A-Sec1 28
"""

import sys
import os
import csv
import json
import copy
import time
import subprocess
import concurrent.futures
import argparse

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library is required. Install it with:")
    print("       pip install requests")
    sys.exit(1)

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICES_CSV = os.path.join(SCRIPT_DIR, "deviceList.csv")
TEMPLATE_DIR = SCRIPT_DIR

ADAPTER_NAME = "Ethernet 6"                      # Windows network adapter name
DEFAULT_DEVICE_IP = "192.168.7.3"              # Factory default Cortex IP
LAPTOP_DEFAULT_IP = "192.168.7.100"            # Laptop IP for default subnet
LAPTOP_SUBNET_MASK = "255.255.255.0"           # Subnet mask for all connections
UPLOAD_ENDPOINT = "/fileupload"
UPLOAD_FILENAME = "Cortexsettings.json"        # Name sent with the upload                       # Seconds to wait after upload
CONNECTION_TIMEOUT = 5                          # Seconds before connection gives up
NETWORK_SETTLE_TIME = 10                         # Seconds to wait after changing adapter
PINGALL_TIMEOUT = 0.4                            # Seconds per IP for subnet scan
PINGALL_WORKERS = 32                             # Concurrent workers for pingall scan
DIAG_TIMEOUT = 0.3                               # Seconds per IP for diag scan
DIAG_WORKERS = 32                                # Concurrent workers for diag scan
VALID_PANEL_TYPES = ["14", "28", "30"]
DEFAULT_SUBNET_BASE = ".".join(DEFAULT_DEVICE_IP.split(".")[:3])
RDP_MODE = False


# ──────────────────────────────────────────────
# Network Adapter Management
# ──────────────────────────────────────────────
def set_adapter_ip(ip, mask, adapter=ADAPTER_NAME):
    """Set a static IP on the Windows Ethernet adapter using netsh."""
    if RDP_MODE:
        print(f"  [RDP] Skipping adapter change: {adapter} → {ip} / {mask}")
        return True

    print(f"  Setting {adapter} to {ip} / {mask} ...")

    cmd = f'netsh interface ip set address name="{adapter}" static {ip} {mask}'

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            print(f"  ✗ Failed to set adapter IP: {error_msg}")
            return False

        print(f"  ✓ Adapter set to {ip}")
        print(f"  Waiting {NETWORK_SETTLE_TIME}s for adapter to settle...")
        time.sleep(NETWORK_SETTLE_TIME)
        return True

    except subprocess.TimeoutExpired:
        print(f"  ✗ netsh command timed out.")
        return False
    except Exception as e:
        print(f"  ✗ Error setting adapter: {e}")
        return False


def derive_laptop_ip(device_ip):
    """
    Derive a laptop IP on the same subnet as the device.
    Uses .254 of the device's subnet (e.g., 10.8.250.111 → 10.8.250.254).
    If the device IS .254, uses .253 to avoid conflict.
    """
    octets = device_ip.split(".")
    if octets[3] == "254":
        octets[3] = "253"
    else:
        octets[3] = "254"
    return ".".join(octets)


def subnet_base_from_ip(ip_address):
    """Return the /24 base (a.b.c) from an IPv4 address, or None if invalid."""
    octets = ip_address.split(".")
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
        return None
    return ".".join(octets[:3])


def laptop_ip_for_subnet(base):
    """Return a laptop IP for the given /24 base."""
    if base == DEFAULT_SUBNET_BASE:
        return LAPTOP_DEFAULT_IP
    return f"{base}.254"


def collect_subnets_from_devices():
    """Collect unique /24 subnet bases from devices.csv."""
    bases = set()
    if not os.path.exists(DEVICES_CSV):
        return bases

    with open(DEVICES_CSV, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ip_address = (row.get("ip_address") or "").strip()
            base = subnet_base_from_ip(ip_address)
            if base:
                bases.add(base)

    return bases


def build_subnet_scan_order(device_ip):
    """Return subnet bases ordered: device subnet first, then others (excluding default)."""
    device_base = subnet_base_from_ip(device_ip)
    bases = collect_subnets_from_devices()
    order = []
    if device_base and device_base != DEFAULT_SUBNET_BASE:
        order.append(device_base)
    for base in sorted(bases):
        if base not in {device_base, DEFAULT_SUBNET_BASE}:
            order.append(base)
    return order


# ──────────────────────────────────────────────
# Device Restart
# ──────────────────────────────────────────────
def restart_device(target_ip):
    """
    Trigger device restart via web API.
    Returns True if restart command was sent successfully (including connection aborts during restart).
    """
    try:
        url = f"http://{target_ip}/restart"
        data = {"restart": "restart"}

        print(f"  Sending restart command to {target_ip}...")
        response = requests.post(url, json=data, timeout=10)

        if response.status_code == 200:
            print(f"  ✓ Restart command sent successfully")
            return True
        else:
            print(f"  ✗ Restart command failed with status: {response.status_code}")
            return False

    except requests.exceptions.Timeout:
        # Some devices stop responding immediately after receiving restart
        print(f"  ✓ Restart command sent (device restarting - response timed out)")
        return True
    except requests.exceptions.ConnectionError as e:
        # Connection abort is expected when device restarts immediately
        if "Connection aborted" in str(e) or "forcibly closed" in str(e):
            print(f"  ✓ Restart command sent (device restarting - connection closed)")
            return True
        else:
            print(f"  ✗ Failed to send restart command: {e}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Failed to send restart command: {e}")
        return False


# ──────────────────────────────────────────────
# Connectivity Test
# ──────────────────────────────────────────────
def ping_device(target_ip, count=1):
    """
    Ping a device to verify basic connectivity.
    Returns True if ping succeeds, False otherwise.
    """
    try:
        # Windows ping command: ping -n <count> <ip>
        cmd = f"ping -n {count} {target_ip}"
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            print(f"  ✓ Ping successful to {target_ip}")
            return True
        else:
            print(f"  ✗ Ping failed to {target_ip}")
            return False

    except subprocess.TimeoutExpired:
        print(f"  ✗ Ping timed out for {target_ip}")
        return False
    except Exception as e:
        print(f"  ✗ Ping error: {e}")
        return False


def verify_device_at_ip(target_ip):
    """
    Verify that a device is responding at the target IP after restart.
    Returns True if device responds with HTTP, False otherwise.
    """
    try:
        url = f"http://{target_ip}"
        response = requests.get(url, timeout=5)
        
        if response.status_code == 200:
            # Check if it's actually a Cortex device
            content = response.text.lower()
            if any(indicator in content for indicator in ['cortex', 'fileupload', 'system.html']):
                print(f"  ✓ Device verified at {target_ip} after restart")
                return True
            else:
                print(f"  ⚠ Device responding at {target_ip} but not identified as Cortex")
                return False
        else:
            print(f"  ✗ Device at {target_ip} returned status {response.status_code}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"  ✗ No response from device at {target_ip}")
        return False


def get_mac_from_arp(target_ip):
    """Return MAC address from ARP table for the given IP, or None."""
    try:
        result = subprocess.run(
            f"arp -a {target_ip}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout
        for line in output.splitlines():
            if target_ip in line:
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
        return None
    except Exception:
        return None


def scan_subnet_for_cortex_base(base):
    """
    Scan a /24 subnet (x.x.x.1-255) with a quick HTTP GET.
    Returns a list of IPs that responded.
    """
    if not base:
        print("  ✗ Scan skipped: invalid device IP format.")
        return []

    print(f"\n  → Scanning subnet {base}.0/24 for Cortex...")

    def probe_ip(target_ip):
        try:
            response = requests.get(f"http://{target_ip}", timeout=PINGALL_TIMEOUT)
            if response.status_code:
                content = response.text.lower()
                is_cortex = any(indicator in content for indicator in ["cortex", "fileupload", "system.html"])
                mac = get_mac_from_arp(target_ip)
                return {
                    "ip": target_ip,
                    "status": response.status_code,
                    "is_cortex": is_cortex,
                    "mac": mac,
                }
        except requests.exceptions.RequestException:
            return None
        return None

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=PINGALL_WORKERS) as executor:
        futures = [
            executor.submit(probe_ip, f"{base}.{last}")
            for last in range(1, 256)
        ]

        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if not result:
                continue
            mac_display = result["mac"] if result["mac"] else "unknown"
            if result["is_cortex"]:
                print(
                    f"  ✓ Cortex-like response at {result['ip']} "
                    f"(status {result['status']}, mac {mac_display})"
                )
            else:
                print(
                    f"  ✓ Response at {result['ip']} "
                    f"(status {result['status']}, mac {mac_display})"
                )
            found.append(result["ip"])

    if not found:
        print("  ✗ Scan complete: no responses found on this subnet.")
    return found


def find_cortex_ips_on_subnet_base(base):
    """
    Quickly find Cortex-like responses on a /24 subnet.
    Returns a list of matching IPs.
    """
    if not base:
        print("  ✗ Diag scan skipped: invalid device IP format.")
        return []

    print(f"\n  → Diag scan on subnet {base}.0/24 ...")

    def probe_ip(target_ip):
        try:
            response = requests.get(f"http://{target_ip}", timeout=DIAG_TIMEOUT)
            if response.status_code:
                content = response.text.lower()
                if any(indicator in content for indicator in ["cortex", "fileupload", "system.html"]):
                    return target_ip
        except requests.exceptions.RequestException:
            return None
        return None

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=DIAG_WORKERS) as executor:
        futures = [
            executor.submit(probe_ip, f"{base}.{last}")
            for last in range(1, 256)
        ]

        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                print(f"  ✓ Cortex-like response at {result}")
                found.append(result)

    if not found:
        print("  ✗ Diag scan complete: no Cortex response found on this subnet.")
    elif len(found) > 1:
        print(f"  ⚠ Multiple Cortex responders found: {', '.join(found)}")
    return found


def restart_and_verify(upload_ip, device_ip, switch_to_device_subnet):
    """Restart the device and verify it at the configured IP."""
    if not restart_device(upload_ip):
        print("  ✗ Failed to trigger restart")
        return False

    restart_countdown = 15
    print(f"  Waiting {restart_countdown}s for device restart...")

    if switch_to_device_subnet:
        print(f"  → Switching to device subnet while device restarts...\n")
        laptop_ip = derive_laptop_ip(device_ip)
        if not set_adapter_ip(laptop_ip, LAPTOP_SUBNET_MASK):
            print("  ✗ Could not configure adapter for device subnet.")
            return False

        remaining_wait = max(0, restart_countdown - NETWORK_SETTLE_TIME)
        for i in range(remaining_wait, 0, -1):
            print(f"  Restarting in {i}s...", end='\r')
            time.sleep(1)
    else:
        for i in range(restart_countdown, 0, -1):
            print(f"  Restarting in {i}s...", end='\r')
            time.sleep(1)

    print("  ✓ Restart countdown complete")

    if verify_device_at_ip(device_ip):
        print(f"  ✓ Device successfully restarted at configured IP: {device_ip}")
        return True

    print(f"  ⚠ Device not verified at {device_ip} after restart")
    return False


# ──────────────────────────────────────────────
# Device Lookup
# ──────────────────────────────────────────────
def load_device(device_name):
    """Look up a device by name in devices.csv and return its info."""
    if not os.path.exists(DEVICES_CSV):
        print(f"  ✗ ERROR: devices.csv not found at:\n    {DEVICES_CSV}")
        sys.exit(1)

    with open(DEVICES_CSV, "r", newline="") as f:
        reader = csv.DictReader(f)

        # Validate CSV headers
        required_headers = {"device_name", "device_type", "ip_address"}
        if not required_headers.issubset(set(reader.fieldnames or [])):
            print(f"  ✗ ERROR: devices.csv must have headers: {required_headers}")
            sys.exit(1)

        for row in reader:
            if row["device_name"].strip() == device_name.strip():
                device = {
                    "device_name": row["device_name"].strip(),
                    "device_type": row["device_type"].strip(),
                    "ip_address": row["ip_address"].strip(),
                }

                # Validate IP format
                octets = device["ip_address"].split(".")
                if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
                    print(f"  ✗ ERROR: Invalid IP address '{device['ip_address']}' "
                          f"for '{device_name}'.")
                    sys.exit(1)

                return device

    print(f"  ✗ ERROR: Device '{device_name}' not found in devices.csv")
    sys.exit(1)


def update_device_type(device_name, device_type):
    """Update the device_type for a device in devices.csv."""
    if not os.path.exists(DEVICES_CSV):
        print(f"  ✗ ERROR: devices.csv not found at:\n    {DEVICES_CSV}")
        sys.exit(1)

    rows = []
    updated = False
    with open(DEVICES_CSV, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row["device_name"].strip() == device_name.strip():
                row["device_type"] = device_type
                updated = True
            rows.append(row)

    if not updated:
        print(f"  ✗ ERROR: Device '{device_name}' not found in devices.csv")
        sys.exit(1)

    with open(DEVICES_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  ✓ Updated device_type for '{device_name}' to '{device_type}' in devices.csv")


# ──────────────────────────────────────────────
# Gateway Derivation
# ──────────────────────────────────────────────
def derive_gateway(ip_address):
    """Replace the last octet with .1 to get the gateway."""
    octets = ip_address.split(".")
    octets[3] = "1"
    return ".".join(octets)


# ──────────────────────────────────────────────
# Template Loading
# ──────────────────────────────────────────────
def load_template(device_type):
    """Load the Cortexsettings template JSON for the given panel type."""
    template_name = f"Cortexsettings ({device_type}).json"
    template_path = os.path.join(TEMPLATE_DIR, template_name)

    if not os.path.exists(template_path):
        print(f"  ✗ ERROR: Template not found:\n    {template_path}")
        sys.exit(1)

    with open(template_path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  ✗ ERROR: Invalid JSON in template: {e}")
            sys.exit(1)


def update_config(config, ip_address, gateway):
    """Update the ipv4.address and ipv4.gateway fields in the config."""
    config = copy.deepcopy(config)

    if "ipv4" not in config:
        print("  ✗ ERROR: Template JSON is missing the 'ipv4' section.")
        sys.exit(1)

    config["ipv4"]["address"] = ip_address
    config["ipv4"]["gateway"] = gateway

    return config


# ──────────────────────────────────────────────
# Upload (single attempt)
# ──────────────────────────────────────────────
def attempt_upload(target_ip, config_data):
    """
    Attempt to upload config JSON to a single IP.

    Returns:
        "success"      — upload worked
        "no_connect"   — could not reach the device
        "bad_response" — connected but upload wasn't confirmed
    """
    url = f"http://{target_ip}{UPLOAD_ENDPOINT}"
    json_bytes = json.dumps(config_data, indent=2).encode("utf-8")

    # Validate size (must be < 1 MB)
    size_mb = len(json_bytes) / (1024 * 1024)
    if size_mb >= 1:
        print(f"  ✗ ERROR: Config file is {size_mb:.2f} MB (must be < 1 MB).")
        return "bad_response"

    # Build multipart form data matching the web UI: field name = 'our-file'
    files = {
        "our-file": (UPLOAD_FILENAME, json_bytes, "application/json")
    }

    try:
        print(f"  Uploading to {url} ...")
        print(f"  [API] Payload size: {size_mb*1024:.2f} KB")
        response = requests.post(url, files=files, timeout=CONNECTION_TIMEOUT)
        
        print(f"  [API] Response status code: {response.status_code}")
        print(f"  [API] Response text: {response.text[:300]}")
        
        response.raise_for_status()

        if "Uploaded" in response.text:
            print(f"  ✓ Upload successful via {target_ip}!")
            return "success"
        else:
            print(f"  ⚠ Unexpected response: {response.text[:200]}")
            return "bad_response"

    except requests.exceptions.ConnectionError as e:
        print(f"  [API] Connection error: {e}")
        return "no_connect"
    except requests.exceptions.Timeout as e:
        print(f"  [API] Timeout: {e}")
        return "no_connect"
    except requests.exceptions.HTTPError as e:
        print(f"  [API] HTTP error: {e}")
        return "bad_response"
    except requests.exceptions.RequestException as e:
        print(f"  ✗ Upload error: {e}")
        return "bad_response"


# ──────────────────────────────────────────────
# Smart Upload — try default IP, then device IP
# ──────────────────────────────────────────────
def upload_config(device_ip, config_data, prefer_device_subnet=False):
    """
    Two-stage upload with automatic Ethernet switching and restart:
      1. Set adapter to default subnet → verify Cortex device → upload config → trigger restart
      2. During 15s restart countdown, switch to device subnet
      3. After restart completes, verify device at configured IP
      4. Keep adapter at final configuration
    """

    if prefer_device_subnet:
        print("\n  → Skipping default IP attempt. Starting with device subnet...")
        return attempt_device_ip(device_ip, config_data)

    # ── Attempt 1: Default IP ──
    print(f"\n  ── Attempt 1: Default IP ({DEFAULT_DEVICE_IP}) ──")

    if not set_adapter_ip(LAPTOP_DEFAULT_IP, LAPTOP_SUBNET_MASK):
        print(f"  ✗ Could not configure adapter for default subnet.")
        return False

    # Test connectivity and verify it's a Cortex device
    if ping_device(DEFAULT_DEVICE_IP):
        if verify_device_at_ip(DEFAULT_DEVICE_IP):
            result = attempt_upload(DEFAULT_DEVICE_IP, config_data)

            if result == "success":
                # Upload succeeded - brief pause then trigger restart
                print(f"  → Config uploaded successfully. Preparing for restart...\n")
                time.sleep(1)  # Brief pause before restart

                if restart_device(DEFAULT_DEVICE_IP):
                    restart_countdown = 15
                    print(f"  Waiting {restart_countdown}s for device restart...")

                    # Switch to device subnet during the restart window
                    print(f"  → Switching to device subnet while device restarts...\n")
                    laptop_ip = derive_laptop_ip(device_ip)

                    if not set_adapter_ip(laptop_ip, LAPTOP_SUBNET_MASK):
                        print(f"  ✗ Could not configure adapter for device subnet.")
                        print(f"  ✓ Upload and restart completed but verification failed.")
                        return True

                    # Finish any remaining restart wait time after adapter settles
                    remaining_wait = max(0, restart_countdown - NETWORK_SETTLE_TIME)
                    for i in range(remaining_wait, 0, -1):
                        print(f"  Restarting in {i}s...", end='\r')
                        time.sleep(1)
                    print(f"  ✓ Restart countdown complete")

                    # Verify the device at its configured IP
                    if verify_device_at_ip(device_ip):
                        print(f"  ✓ Device successfully restarted at configured IP: {device_ip}")
                        print(f"  ✓ Adapter is now configured for: {laptop_ip}")
                        return True
                    else:
                        print(f"  ⚠ Device not verified at {device_ip} after restart")
                        print(f"  ✓ Upload and restart completed. Adapter configured for: {laptop_ip}")
                        return True
                else:
                    print(f"  ✗ Failed to trigger restart")
                    return False

        else:
            print(f"  ✗ Device at {DEFAULT_DEVICE_IP} is not a Cortex panel")
            # Continue to attempt 2
    else:
        print(f"  ✗ No device responding at default IP ({DEFAULT_DEVICE_IP})")
        # Continue to attempt 2

    # Default IP check failed - try device IP directly
    print(f"  → Attempting direct connection to {device_ip}...\n")

    if attempt_device_ip(device_ip, config_data):
        return True

    # Both attempts failed
    print(f"\n  ✗ FAILED: Could not find and configure Cortex device.")
    print(f"    • Checked: {DEFAULT_DEVICE_IP} (default subnet)")
    print(f"    • Checked: {device_ip} (device subnet)")
    print(f"    • Verify the device is powered on and network-connected.")
    return False


def attempt_device_ip(device_ip, config_data):
    """Attempt upload and restart using the device's assigned IP/subnet."""
    # ── Attempt 2: Device's assigned IP ──
    print(f"  ── Attempt 2: Device IP ({device_ip}) ──")

    laptop_ip = derive_laptop_ip(device_ip)

    if not set_adapter_ip(laptop_ip, LAPTOP_SUBNET_MASK):
        print(f"  ✗ Could not configure adapter for device subnet.")
        return False

    # Test connectivity and verify it's a Cortex device
    if ping_device(device_ip):
        if verify_device_at_ip(device_ip):
            result = attempt_upload(device_ip, config_data)

            if result == "success":
                # Upload succeeded - brief pause then trigger restart
                print(f"  → Config uploaded successfully. Preparing for restart...\n")
                time.sleep(1)  # Brief pause before restart

                if restart_device(device_ip):
                    restart_countdown = 15
                    print(f"  Waiting {restart_countdown}s for device restart...")

                    # Adapter is already on the device subnet; just wait out restart window
                    remaining_wait = max(0, restart_countdown)
                    for i in range(remaining_wait, 0, -1):
                        print(f"  Restarting in {i}s...", end='\r')
                        time.sleep(1)
                    print(f"  ✓ Restart countdown complete")

                    # Device should now be at its configured IP - verify
                    if verify_device_at_ip(device_ip):
                        print(f"  ✓ Device successfully restarted at {device_ip}")
                        print(f"  ✓ Adapter is now configured for: {laptop_ip}")
                        return True
                    else:
                        print(f"  ⚠ Device not verified at {device_ip} after restart")
                        return True
                else:
                    print(f"  ✗ Failed to trigger restart")
                    return False
        else:
            print(f"  ✗ Device at {device_ip} is not a Cortex panel")
    else:
        print(f"  ✗ No device responding at {device_ip}")

    return False


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Upload Cortex configuration to a device.",
        usage="python upload_cortex.py <device_name> [device_type]",
    )
    parser.add_argument(
        "device_name",
        help="Device name as listed in devices.csv",
    )
    parser.add_argument(
        "device_type",
        nargs='?',
        help="Device type (14, 28, or 30) if not specified in devices.csv",
    )
    parser.add_argument(
        "--a2",
        action="store_true",
        help="Start with the device subnet (skip default IP attempt)",
    )
    parser.add_argument(
        "--pingall",
        action="store_true",
        help="Scan the device subnet for a Cortex response and report the IP",
    )
    parser.add_argument(
        "--diag",
        action="store_true",
        help="Find current IP, upload corrected config, restart, and verify",
    )
    parser.add_argument(
        "--rdp",
        action="store_true",
        help="Run without changing local Ethernet settings",
    )
    args = parser.parse_args()

    global RDP_MODE
    RDP_MODE = args.rdp

    print(f"\n{'='*50}")
    print(f"  CORTEX CONFIG UPLOADER")
    print(f"{'='*50}\n")

    # 1 — Look up device
    device = load_device(args.device_name)
    ip_address = device["ip_address"]
    device_type = device["device_type"]

    if not device_type:
        if args.device_type:
            if args.device_type not in VALID_PANEL_TYPES:
                print(f"  ✗ ERROR: Invalid device_type '{args.device_type}'. Must be one of: {VALID_PANEL_TYPES}")
                sys.exit(1)
            update_device_type(args.device_name, args.device_type)
            device_type = args.device_type
        else:
            print(f"  ✗ ERROR: Panel '{args.device_name}' does not have defined device_type. Please run again with: python upload_cortex.py {args.device_name} {{device_type}}")
            sys.exit(1)

    # Validate panel type
    if device_type not in VALID_PANEL_TYPES:
        print(f"  ✗ ERROR: Invalid device_type '{device_type}' "
              f"for '{args.device_name}'. Must be one of: {VALID_PANEL_TYPES}")
        sys.exit(1)

    gateway = derive_gateway(ip_address)

    print(f"  Device:      {device['device_name']}")
    print(f"  Type:        {device_type}-panel")
    print(f"  IP Address:  {ip_address}")
    print(f"  Gateway:     {gateway}")
    print(f"  Template:    Cortexsettings ({device_type}).json")
    print(f"  Default IP:  {DEFAULT_DEVICE_IP}")

    if args.pingall:
        print("\n  → Running subnet scan (--pingall)")
        subnet_order = build_subnet_scan_order(ip_address)

        for base in subnet_order:
            laptop_ip = laptop_ip_for_subnet(base)
            if not set_adapter_ip(laptop_ip, LAPTOP_SUBNET_MASK):
                print(f"  ✗ Could not configure adapter for subnet {base}.0/24.")
                sys.exit(1)

            found_ips = scan_subnet_for_cortex_base(base)
            if found_ips:
                print(f"  ✓ Scan complete. Responsive IPs: {', '.join(found_ips)}")
                sys.exit(0)

        print("  → No responses found on deviceList subnets. Trying default subnet...")
        if not set_adapter_ip(LAPTOP_DEFAULT_IP, LAPTOP_SUBNET_MASK):
            print("  ✗ Could not configure adapter for default subnet.")
            sys.exit(1)

        found_ips = scan_subnet_for_cortex_base(DEFAULT_SUBNET_BASE)
        if found_ips:
            print(f"  ✓ Scan complete. Responsive IPs: {', '.join(found_ips)}")
            sys.exit(0)
        sys.exit(1)

    # 2 — Load template
    config = load_template(device_type)

    # 3 — Inject device-specific IP & gateway
    config = update_config(config, ip_address, gateway)
    print(f"\n  ✓ Config updated with device IP & gateway")

    if args.diag:
        print("\n  → Running diag mode (--diag)")
        subnet_order = build_subnet_scan_order(ip_address)
        while True:
            found_any = False
            for base in subnet_order:
                laptop_ip = laptop_ip_for_subnet(base)
                if not set_adapter_ip(laptop_ip, LAPTOP_SUBNET_MASK):
                    print(f"  ✗ Could not configure adapter for subnet {base}.0/24.")
                    sys.exit(1)

                found_ips = find_cortex_ips_on_subnet_base(base)
                if not found_ips:
                    continue

                found_any = True
                if ip_address in found_ips:
                    print(f"  ✓ Correct IP is already set: {ip_address}")
                    sys.exit(0)

                if len(found_ips) > 1:
                    print("  ✗ Multiple Cortex responders found. Aborting diag to avoid wrong upload.")
                    print("  → Isolate the target panel and run --diag again.")
                    rerun = input("  Run diag scan again now? (y/N): ").strip().lower()
                    if rerun in {"y", "yes"}:
                        break
                    sys.exit(1)

                print(f"  ⚠ Found device at {found_ips[0]} (expected {ip_address})")

                found_ip = found_ips[0]
                result = attempt_upload(found_ip, config)
                if result != "success":
                    print("  ✗ Upload failed during diag")
                    sys.exit(1)

                print("  → Config uploaded successfully. Preparing for restart...\n")
                time.sleep(1)

                if restart_and_verify(found_ip, ip_address, switch_to_device_subnet=True):
                    sys.exit(0)
                sys.exit(1)

            if found_any:
                continue
            break

        print("  → No Cortex response found on deviceList subnets. Trying default IP...")
        if not set_adapter_ip(LAPTOP_DEFAULT_IP, LAPTOP_SUBNET_MASK):
            print("  ✗ Could not configure adapter for default subnet.")
            sys.exit(1)

        result = attempt_upload(DEFAULT_DEVICE_IP, config)
        if result != "success":
            print("  ✗ Upload failed on default IP during diag")
            sys.exit(1)

        print("  → Config uploaded successfully. Preparing for restart...\n")
        time.sleep(1)

        if restart_and_verify(DEFAULT_DEVICE_IP, ip_address, switch_to_device_subnet=True):
            sys.exit(0)
        sys.exit(1)

    # 4 — Upload (with automatic adapter switching)
    success = upload_config(ip_address, config, prefer_device_subnet=args.a2)

    if success:
        print(f"  ✓ Done! '{device['device_name']}' configured successfully.")
    else:
        print(f"\n  ✗ Configuration of '{device['device_name']}' failed.")
        sys.exit(1)

    print(f"\n{'='*50}\n")


if __name__ == "__main__":
    main()
