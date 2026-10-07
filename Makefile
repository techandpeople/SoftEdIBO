# SoftEdIBO developer Makefile.
#
# Thin front-end over the existing scripts (scripts/*.sh stay the single source
# of truth, CI calls them directly). Run `make` or `make help` for the list.
#
# Uses ./.venv when it exists, else whatever `python` is on PATH. Override with
# `make PY=/path/to/python ...`.

VENV ?= .venv
ifneq ($(wildcard $(VENV)/bin/python),)
PY ?= $(VENV)/bin/python
else
PY ?= python
endif

PIO := $(PY) -m platformio

# Firmware flash/monitor defaults - override on the command line, e.g.
#   make flash BOARD=node_actuator ENV=multiplexed_rgbw PORT=/dev/ttyACM0
BOARD ?= node_actuator
ENV   ?= direct_rgbw
PORT  ?=
PORT_ARG := $(if $(PORT),--upload-port $(PORT),)
MON_PORT_ARG := $(if $(PORT),--port $(PORT),)

# Qt Designer sources -> generated ui_*.py (incremental: only stale files rebuild).
UI_SRC := $(wildcard src/gui/ui/*.ui)
UI_PY  := $(patsubst src/gui/ui/%.ui,src/gui/ui_%.py,$(UI_SRC))
UIC    ?= $(firstword $(wildcard $(VENV)/bin/pyside6-uic) pyside6-uic)

BLOCKLY := src/gui/blockly/blockly.min.js

.DEFAULT_GOAL := help

.PHONY: help sys-deps venv install install-dev ui run run-debug run-wayland test typecheck check \
        blockly firmware fw-gateway fw-actuator fw-magnet fw-thymio \
        flash flash-nodes erase monitor bundle clean clean-ui \
        usb-list attach attach-auto detach copy-firmware-to-win

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Firmware vars: BOARD=$(BOARD) ENV=$(ENV) PORT=$(PORT)"

# --- Python environment -------------------------------------------------------

# System libraries Qt's xcb platform plugin needs (Debian/Ubuntu/WSL). Without
# them the app aborts with "Could not load the Qt platform plugin xcb" - Qt
# blames libxcb-cursor0 even when the missing one is another lib on this list.
QT_SYS_DEPS := libgl1 libegl1 libglib2.0-0 libdbus-1-3 \
	libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
	libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
	libxcb-xkb1 libxkbcommon-x11-0 libfontconfig1

sys-deps: ## Install the Qt system libraries via apt (needs sudo)
	sudo apt-get install -y $(QT_SYS_DEPS)

$(VENV)/bin/python:
	python -m venv $(VENV)

venv: $(VENV)/bin/python ## Create the .venv

install: venv ## Install the app dependencies into .venv
	$(VENV)/bin/pip install -r requirements.txt

install-dev: install ## Also install dev + ML + Thymio extras, PlatformIO and PyInstaller
	$(VENV)/bin/pip install -e ".[dev,ml,thymio]" "platformio==6.1.19" pyinstaller

# --- App ----------------------------------------------------------------------

src/gui/ui_%.py: src/gui/ui/%.ui
	@echo "  $< => $@"
	@$(UIC) $< -o $@

ui: $(UI_PY) ## Compile the .ui files (only the changed ones)

run: ui ## Run the app
	$(PY) scripts/run.py

run-debug: ui ## Run the app with --debug logging
	$(PY) scripts/run.py --debug

run-wayland: ui ## Run the app on native Wayland (WSLg) instead of xcb
	QT_QPA_PLATFORM=wayland $(PY) scripts/run.py

test: ui ## Run the test suite (headless)
	QT_QPA_PLATFORM=offscreen $(PY) -m pytest -q

typecheck: ui ## Run pyright
	$(PY) -m pyright

check: test typecheck ## Tests + type check

$(BLOCKLY):
	bash scripts/fetch_blockly.sh

blockly: $(BLOCKLY) ## Vendor Blockly for the behaviour editor (once, needs internet)

bundle: ui $(BLOCKLY) ## Build the PyInstaller bundle into dist/
	$(PY) -m PyInstaller softedibo.spec

# --- Firmware -----------------------------------------------------------------

firmware: ## Build every firmware .bin the app/wizard/OTA ships
	scripts/build-firmware.sh

fw-gateway: ## Build only the gateway firmware
	scripts/build-firmware.sh gateway

fw-actuator: ## Build only the actuator firmware (direct + multiplexed)
	scripts/build-firmware.sh actuator

fw-magnet: ## Build only the magnet-sensor firmware
	scripts/build-firmware.sh magnet

fw-thymio: ## Build only the Thymio RCP (C6) firmware
	scripts/build-firmware.sh thymio

flash: ## Build + USB-flash BOARD/ENV (optionally PORT=...)
	$(PIO) run -d firmware/$(BOARD) -e $(ENV) -t upload $(PORT_ARG)

erase: ## Erase BOARD/ENV flash (recovers a failed-OTA boot loop)
	$(PIO) run -d firmware/$(BOARD) -e $(ENV) -t erase $(PORT_ARG)

monitor: ## Serial monitor for BOARD/ENV (optionally PORT=...)
	$(PIO) device monitor -d firmware/$(BOARD) -e $(ENV) $(MON_PORT_ARG)

# Over-the-air update of every online node through the gateway (dev mirror of
# Tools -> Update Nodes (OTA)...; close the app first, it holds the serial port).
#   make flash-nodes LED=rgb            force the RGB build (LED=auto|rgb|rgbw)
#   make flash-nodes OTA=espnow         slow transport, no gateway access point
#   make flash-nodes FW_DEBUG=1 DRY=1   debug builds; DRY=1 only lists the plan
#   make flash-nodes TYPE=node_direct   also flash online nodes no robot lists,
#                                       as that board type
LED      ?= auto
TYPE     ?=
OTA      ?= wifi
FW_DEBUG ?=
DRY      ?=

flash-nodes: ## OTA-flash every online node via the gateway (LED=auto|rgb|rgbw, OTA=wifi|espnow)
	$(PY) scripts/ota_nodes.py --led $(LED) --transport $(OTA) \
		$(if $(TYPE),--type $(TYPE),) $(if $(FW_DEBUG),--debug,) \
		$(if $(DRY),--dry-run,) $(if $(PORT),--port $(PORT),)

# --- WSL USB passthrough (usbipd-win) -----------------------------------------
#
# Forwards the gateway's USB from Windows into WSL. Selected by hardware id
# (GW_HWID, the ESP32-S3/C6 native USB) or, when several boards share that id,
# by GW_BUSID from `make usb-list`, e.g. `make attach GW_BUSID=3-2`.
# The one-time `bind` needs admin, so it goes through a UAC prompt.

USBIPD   ?= usbipd.exe
GW_HWID  ?= 303a:1001
GW_BUSID ?=
GW_SEL   := $(if $(GW_BUSID),--busid $(GW_BUSID),--hardware-id $(GW_HWID))
GW_MATCH := $(if $(GW_BUSID),^$(GW_BUSID) ,$(GW_HWID))

usb-list: ## List the Windows USB devices usbipd can forward
	@$(USBIPD) list

define gw_bind
	@if $(USBIPD) list | tr -d '\r' | grep -E '$(GW_MATCH)' | grep -q 'Not shared'; then \
		echo "Binding $(GW_SEL) (accept the UAC prompt on Windows)..."; \
		powershell.exe -NoProfile -Command "Start-Process '$(USBIPD)' -Verb RunAs -Wait -ArgumentList 'bind $(GW_SEL)'"; \
	fi
endef

attach: ## Attach the gateway USB to WSL (binds first if needed)
	$(gw_bind)
	@if $(USBIPD) list | tr -d '\r' | grep -E '$(GW_MATCH)' | grep -q 'Attached'; then \
		echo "Gateway already attached to WSL."; \
	else \
		$(USBIPD) attach --wsl $(GW_SEL); \
	fi

attach-auto: ## Keep the gateway attached across resets/re-plugs (blocks; Ctrl+C stops)
	$(gw_bind)
	$(USBIPD) attach --wsl --auto-attach $(GW_SEL)

detach: ## Give the gateway USB back to Windows
	$(USBIPD) detach $(GW_SEL)

# --- Copy firmware into the Windows app ---------------------------------------
#
# Drops the freshly built firmware/*/*.bin into the Windows app's bundle, so its
# wizard/OTA flash them without waiting for a release. The bundle is located
# through what the app itself left in %LOCALAPPDATA%\SoftEdIBO: bundle_path.txt
# (written at every start, see src/app_paths.py) or, for a build that predates
# it, the install folder named in the self-updater's update.log.
# Override with WIN_BUNDLE=<app folder>/_internal (a WSL path).

WIN_BUNDLE ?=

copy-firmware-to-win: ## Copy the built firmware .bin files into the Windows app
	@bundle="$(WIN_BUNDLE)"; \
	if [ -z "$$bundle" ]; then \
		state="$$(wslpath "$$(cmd.exe /C 'echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r')")/SoftEdIBO"; \
		win=""; \
		if [ -f "$$state/bundle_path.txt" ]; then \
			win="$$(tr -d '\r' < "$$state/bundle_path.txt")"; \
		elif [ -f "$$state/update.log" ]; then \
			win="$$(tr -d '\r' < "$$state/update.log" | sed -n 's/^.* Updating //p' | tail -n 1)"; \
			[ -n "$$win" ] && win="$$win\\_internal"; \
		fi; \
		[ -n "$$win" ] && bundle="$$(wslpath "$$win")"; \
	fi; \
	if [ -z "$$bundle" ] || [ ! -d "$$bundle/firmware" ]; then \
		echo "Windows app not found ($${bundle:-no record of it}). Start it once, or pass WIN_BUNDLE=<app folder>/_internal."; \
		exit 1; \
	fi; \
	echo "Copying firmware => $$bundle/firmware"; \
	cd firmware && cp -v --parents */*.bin "$$bundle/firmware/"

# --- Cleanup ------------------------------------------------------------------

clean-ui: ## Delete the generated ui_*.py files
	rm -f src/gui/ui_*.py

clean: clean-ui ## Delete generated files and Python/PyInstaller build output
	rm -rf build dist .pytest_cache
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -not -path "./firmware/*" -prune -exec rm -rf {} +
