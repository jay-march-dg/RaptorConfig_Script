# Raptor Cortex Upload Script

This script uploads device-specific JSON configuration files to Cortex panels automatically, managing Windows Ethernet adapter settings in the process.

## Prerequisites

- **Python 3.6+** installed
- **Windows OS** (script uses Windows-specific network commands)
- **Administrator privileges** (required when the script changes adapter settings)

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/jay-march-dg/RaptorConfig_Script.git
   cd RaptorConfig_Script
   ```

2. Install required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

**⚠️ IMPORTANT: Run as Administrator when adapter settings are changed**

### Basic Usage
```bash
python upload_cortex.py <device_name>
```

### Examples
```bash
# Upload config for a device that has device_type defined
python upload_cortex.py 4E11-G01A-Sec1

# Upload config for a device missing device_type (will prompt for type)
python upload_cortex.py 4C11-R01A-Sec1 28

# Skip default IP attempt and start on the device subnet
python upload_cortex.py 4E12-G03C-Sec2 14 --a2

# Scan subnets and list responders
python upload_cortex.py 4E12-G03C-Sec2 14 --pingall

# Diagnose a panel with incorrect IP
python upload_cortex.py 4E12-G03C-Sec2 14 --diag
```

### Device Types
- `14` - 14-panel configuration
- `28` - 28-panel configuration
- `30` - 30-panel configuration

### Flags
- `--a2` - Start with the device subnet (skip default IP attempt)
- `--pingall` - Scan subnets for HTTP responders and show MACs when available
- `--diag` - Diagnose a panel with incorrect IP, upload corrected config, restart, and verify

## How It Works

1. **Device Lookup**: Reads device information from `deviceList.csv`
2. **Configuration Loading**: Loads the appropriate `Cortexsettings ({type}).json` template
3. **Network Management**: Automatically switches network adapters to communicate with devices
4. **Upload Process**: Uploads configuration via HTTP to the Cortex panel
5. **Verification**: Confirms successful configuration and device restart

## Subnet Scans

Both `--pingall` and `--diag` scan subnets in this order:

1. The device's expected subnet
2. Other /24 subnets found in `deviceList.csv`
3. The default subnet (192.168.7.x)

`--pingall` lists all responding IPs on each subnet. `--diag` scans for Cortex-like responses; if more than one is found on a subnet, it pauses and asks you to rerun the scan after isolating the target panel.

## Diag Mode

`--diag` is intended for panels with incorrect IPs:

1. Scan subnets for a Cortex response
2. If the correct IP is already set, stop
3. If one Cortex responder is found, upload the corrected config to that IP
4. Restart and verify the device at the configured IP

## Files

- `upload_cortex.py` - Main upload script
- `deviceList.csv` - Device database with names, types, and IP addresses
- `Cortexsettings (14).json` - Configuration template for 14-panels
- `Cortexsettings (28).json` - Configuration template for 28-panels
- `Cortexsettings (30).json` - Configuration template for 30-panels
- `requirements.txt` - Python dependencies

## Troubleshooting

- **"Access denied" or adapter errors**: Right-click Command Prompt → "Run as administrator"
- **"requests library required"**: Run `pip install requests`
- **Network errors**: Ensure Ethernet adapter is properly configured
- **Device not found**: Check `deviceList.csv` for correct device name

## Security Note

This script modifies network adapter settings and requires administrator privileges. Use only on trusted networks and devices.