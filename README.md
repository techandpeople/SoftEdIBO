# SoftEdIBO

Soft-based robot platform for inclusive education.
Developed at [LASIGE](https://www.lasige.pt/), Faculdade de Ciências, Universidade de Lisboa.

---

## Installation

### Linux (x86-64)

```bash
curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash
```

This will download the latest release, install it to `/opt/SoftEdIBO/`, create a `softedibo` command in your PATH, and add a desktop entry to the application menu.

**Nightly build** (latest commit on `master`):

```bash
curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash -s -- --nightly
```

**Uninstall:**

```bash
softedibo --uninstall 2>/dev/null || /opt/SoftEdIBO/install.sh --uninstall
```

> **First time with USB?** After installing, run:
> ```bash
> sudo usermod -aG dialout $USER
> ```
> Then log out and back in. The installer does this automatically if needed.

---

### Windows (x64)

1. Download **`SoftEdIBO-windows-x64.zip`** from the [latest release](https://github.com/techandpeople/SoftEdIBO/releases/latest)
2. Extract and run **`SoftEdIBO.exe`**

> **USB driver:** install the [CH340](https://www.wch-ic.com/downloads/CH341SER_EXE.html) or [CP210x](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers) driver if your device is not detected.

---

## Usage

On first launch, a setup wizard guides you through flashing the firmware to the ESP32 nodes.

---

## Development

```bash
git clone https://github.com/techandpeople/SoftEdIBO.git
cd SoftEdIBO
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/run.py
```

Requires Python 3.12+.
