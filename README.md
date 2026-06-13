# Cortex Upload Automation

**Purpose**: Upload a device configuration template to a Cortex panel, trigger a restart, and verify the device responds at its configured IP.

## Quick Start

### 1. Install Dependencies
Run the setup script once:
```cmd
setup.bat
```

This installs the Python dependencies needed by the GUI and CLI tools.

### 2. Run the GUI (Recommended)
```cmd
python cortex_gui.py
```

The GUI provides an easy-to-use interface to:
- View and manage the device list
- Upload configurations to devices
- Configure adapter settings
- Run prefix-based verify/upload workflows
- View upload logs and status

### 3. Or Use Command-Line (Advanced)
For scripting or automation, use the CLI:
```cmd
python upload_cortex.py DEVICE_NAME [device_type] [options]
```

## Files

- `cortex_gui.py` - GUI application (recommended for most users)
- `upload_cortex.py` - CLI automation script (for advanced/scripted use)
- `cortex_settings.py` - Shared adapter-name settings helper
- `cortex_adapter_settings.json` - Saved adapter name used by the GUI and CLI
- `deviceList.csv` - Device inventory (columns: `device_name,device_type,ip_address`)
- `Cortexsettings (<device_type>).json` - Configuration template files (for example `Cortexsettings (28).json`)
- `setup.bat` - Automated setup script (run once to install dependencies)
- `requirements.txt` - Python package dependencies

## Prerequisites

- Windows 10 or later
- Python 3.8 or later
- Administrator privileges for network adapter changes via `netsh`
  - Can be skipped with `--rdp`

## How It Works

The upload process:
1. Reads the target device from `deviceList.csv` by device name or prefix
2. Loads the matching `Cortexsettings (<device_type>).json` template
3. Rewrites the template IP and gateway to match the target device subnet
4. Temporarily configures the laptop adapter to reach the panel
5. Uploads the rewritten configuration via multipart POST
6. Triggers a device restart and waits for reboot
7. Reconfigures the adapter if needed for the final subnet
8. Verifies the device responds at its configured IP

## Configuration

### Device List (`deviceList.csv`)

Required columns: `device_name`, `device_type`, `ip_address`

Example:
```csv
device_name,device_type,ip_address
4A11-R06A-Sec1,30,10.8.197.19
4A11-R06A-Sec2,14,10.8.197.20
```

The gateway is derived automatically as `.1` on the device subnet.

### Template Files

- Filename format: `Cortexsettings (<device_type>).json`
- The template should contain the default/subnet values that the script rewrites before upload
- The script updates the IP address and gateway fields to match the selected device

### Adapter Settings

- The GUI includes an editable adapter-name field in the Settings tab
- The CLI supports `--set-adapter NAME` to save the adapter name for later runs
- If no adapter name is set, the script falls back to the last saved value or `Ethernet`

## Command-Line Usage (Advanced)

For scripting or automation, use the CLI directly:

### Normal run
```cmd
python upload_cortex.py 4A11-R06A-Sec1
```

### Provide device type when the CSV row is blank
```cmd
python upload_cortex.py 4A11-R06A-Sec1 30
```

### Verify all devices that match a prefix
```cmd
python upload_cortex.py 4A --verifyall
```

### Verify only one prefix and type
```cmd
python upload_cortex.py 4A 30 --verifyall
```

### Configure all devices that match a prefix and type
```cmd
python upload_cortex.py 4A 30 --configall
```

### Skip adapter changes
```cmd
python upload_cortex.py 4A11-R06A-Sec1 --rdp
```

### Start with the device subnet
```cmd
python upload_cortex.py 4A11-R06A-Sec1 --a2
```

### Save the adapter name
```cmd
python upload_cortex.py --set-adapter "Ethernet 6"
```

### Open the panel web UI
```cmd
python upload_cortex.py 4A11-R06A-Sec1 --open
```

### See all options
```cmd
python upload_cortex.py --help
```

## Flags

- `--a2` - Start with the device subnet instead of the default IP attempt
- `--pingall` - Scan subnets for HTTP responders and report the IPs found
- `--diag` - Find the current IP, upload the corrected config, restart, and verify
- `--rdp` - Run without changing local Ethernet settings
- `--reboot` - Restart the device and verify after reboot
- `--verifyall` - Verify devices by name prefix, optionally filtered by type
- `--configall` - Upload and restart devices by name prefix and type
- `--open` - Open the device IP in a web browser
- `--set-adapter NAME` - Save and use a Windows adapter name

## Environment Variables

This project primarily uses local settings and command-line flags rather than environment variables.

## Troubleshooting

### General Issues

**Python is not installed or not found**
- Install Python 3.8+ from [python.org](https://www.python.org/downloads/)
- Make sure Add Python to PATH is checked during installation

**ModuleNotFoundError: No module named 'PySide6'**
- Run `setup.bat` to install dependencies
- Or install manually with `python -m pip install -r requirements.txt`

**Permission denied or adapter configuration fails**
- Run Command Prompt as Administrator
- Or use `--rdp` to skip adapter changes

### Device Upload Issues

**Device not found by verify/config prefix**
- Check the prefix you entered in `deviceList.csv`
- Use `--verifyall` with the exact prefix and optional type

**Device not reachable after upload**
- Check that the adapter is configured for the target subnet
- Verify the IP address in `deviceList.csv`
- Allow more time for the device to reboot

**Upload page or web UI does not respond**
- Confirm the target device is powered on and reachable
- Try `--diag` to find the correct IP before uploading

## Safety Notes

- The device will reboot after configuration upload
- Perform uploads during a maintenance window
- Ensure the template matches the device type
- Administrator privileges are required for network adapter changes

## Support & Documentation

- Run `python upload_cortex.py --help` for CLI documentation
- Use the GUI settings page to update the adapter name if your Windows adapter changes
- Check device LED status and web interface if uploads fail