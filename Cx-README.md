# Cx README - Cortex Config Upload Script

This guide is for Cx teams using upload_cortex.py to configure and troubleshoot Cortex panels.

IF A PANEL DOES NOT HAVE AN IP YET YOU MUST UNPLUG THE NETWORK DROP, use --pingall to verify all the responders at a port if needed

MAKE SURE THE SCRIPT IS CONFIGURING THE ETHERNET PORT BEING USED ON YOUR LAPTOP (i.e: Ethernet, Ethernet 2, Ethernet 6, etc...) 
- edit this on line 42 in upload_cortex.py if needed, save file and move on.

## Quick Start

Run from the project folder:

```bash
python upload_cortex.py <device_name>
```

If device_type is missing in deviceList.csv, pass it:

```bash
python upload_cortex.py <device_name> <device_type>
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
python upload_cortex.py 4C --verifyall (TO BE USED IN RDP ALONG WITH --rdp COMMAND)
```

Verify all devices with a prefix for a specific type:

```bash
python upload_cortex.py 4C 28 --verifyall (TO BE USED IN RDP ALONG WITH --rdp COMMAND)
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
- --verifyall
  - Verify multiple devices by name prefix (optional device_type filter)
- --open
  - Open the device IP in the default browser

## How Subnet Scans Work

Both --pingall and --diag scan subnets in this order:

1. The device expected subnet
2. Other /24 subnets found in deviceList.csv
3. Default subnet (192.168.7.x)

--pingall lists all HTTP responders. --diag looks for Cortex-like responses.

## Adapter and Admin Notes

- Script uses Windows netsh to change the adapter IP.
- You must run as Administrator when adapter changes are required.
- Adapter name in the script: Ethernet
- Use --rdp if you are connected through RDP or do not want adapter changes.

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
- deviceList.csv
- Cortexsettings (<type>).json
- requirements.txt
