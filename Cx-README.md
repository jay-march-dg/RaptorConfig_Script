# Cx README - Cortex Config Upload Script

This guide is for Cx teams using the Cortex upload tools to configure, verify, and troubleshoot panels.

If a panel does not have an IP yet, unplug the network drop before scanning. Use `--pingall` to verify responders at a port when needed.

Make sure the tool is configured for the Ethernet adapter being used on your laptop. The adapter name is now shared between the GUI and CLI, and it falls back to the last saved value or `Ethernet`.

## Quick Start

### Recommended: GUI

Run the GUI from the project folder:

```bash
python cortex_gui.py
```

The GUI is the easiest way to:

- View and manage `deviceList.csv`
- Run single-device upload, reboot, and verify workflows
- Run prefix-based `--verifyall` and `--configall` operations
- Change the adapter name from the Settings tab
- View upload logs and status

### CLI

Run from the project folder:

```bash
python upload_cortex.py <device_name>
```

If `device_type` is missing in `deviceList.csv`, pass it:

```bash
python upload_cortex.py <device_name> <device_type>
```

To verify or configure by prefix:

```bash
python upload_cortex.py 4A --verifyall
python upload_cortex.py 4A 30 --verifyall
python upload_cortex.py 4A 30 --configall
```

## Device Types

Valid types (must match template file names):

- 14
- 28
- 30
- 26S(3x3)
- 10S(5x5)
- 26S(1x1)

Template files are named like:

- Cortexsettings (14).json
- Cortexsettings (26S(3x3)).json

## Common Tasks

Open the GUI:

```bash
python cortex_gui.py
```

Upload config and restart:

```bash
python upload_cortex.py 4C-R07B-Sec1
```

Upload starting on the device subnet (skip default IP attempt):

```bash
python upload_cortex.py 4C-R07B-Sec1 --a2
```

Verify a single device (HTTP check only):

```bash
python upload_cortex.py 4C-R07B-Sec1 --reboot
```

Verify all devices with a prefix (all types):

```bash
python upload_cortex.py 4C --verifyall
```

Verify all devices with a prefix for a specific type:

```bash
python upload_cortex.py 4C 28 --verifyall
```

Upload config/restart for all devices with a prefix and type:

```bash
python upload_cortex.py 4C 30 --configall
```

Open the device in a browser:

```bash
python upload_cortex.py 4C-R07B-Sec1 --open
```

## Flags Reference

- --a2
  - Start with the device subnet (skip default IP attempt)
- --pingall
  - Scan subnets and list HTTP responders
- --diag
  - Find a panel with the wrong IP, upload the corrected config, restart, and verify
- --rdp
  - Do not change the local Ethernet adapter IP
- --reboot
  - Send restart command and verify the device after reboot
- --configall
  - Upload config/restart for all devices matching a prefix and type
  - WARNING: This will push a new config to all online devices in scope
- --verifyall
  - Verify multiple devices by name prefix (optional device_type filter)
- --open
  - Open the device IP in the default browser
- --set-adapter NAME
  - Save and use a Windows adapter name for future runs

## How Subnet Scans Work

Both --pingall and --diag scan subnets in this order:

1. The device expected subnet
2. Other /24 subnets found in deviceList.csv
3. Default subnet (192.168.7.x)

--pingall lists all HTTP responders. --diag looks for Cortex-like responses.

## Adapter and Admin Notes

- Script uses Windows netsh to change the adapter IP.
- You must run as Administrator when adapter changes are required.
- Adapter name is shared between the GUI and CLI.
- Use the Settings tab in `cortex_gui.py` to update the adapter name.
- Use `--set-adapter NAME` if you want to save the adapter name from the CLI.
- Use `--rdp` if you are connected through RDP or do not want adapter changes.

## Troubleshooting

- Access denied or adapter errors:
  - Run PowerShell or Command Prompt as Administrator.
- Device not found:
  - Confirm deviceList.csv entry and spelling.
- Template not found:
  - Ensure Cortexsettings (<type>).json exists for the device_type.
- Multiple responders in --diag:
  - Isolate the target device on the network and rerun. (UNPLUG NETWORK DROP)
- Browser open not working:
  - Try --open and manually add http:// or https:// in your browser if needed.

## Files

- upload_cortex.py
- cortex_gui.py
- cortex_settings.py
- cortex_adapter_settings.json
- deviceList.csv
- Cortexsettings (<type>).json
- requirements.txt
