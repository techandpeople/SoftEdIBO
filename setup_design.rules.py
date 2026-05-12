import kicad
from kicad.pcbnew import Board


def main():
    # Connect to the local KiCad instance over the socket
    try:
        board = Board.active()  # Connects to the currently open board
        settings = board.design_settings

        # 0.15: MCU, 0.25: Sensing Mesh, 0.5: Power, 1.0: Bus
        settings.track_widths = [0.15, 0.25, 0.5, 1.0]

        # Standard JLC/PCBWay via sizes
        settings.via_sizes = [(0.6, 0.3), (0.8, 0.4), (1.2, 0.6)]

        # This syncs the data back to the KiCad UI safely
        board.commit()
        print("Successfully injected 1.6mm NOREQ design rules via IPC.")
    except Exception as e:
        print(f"Error: Ensure KiCad is open and the API Server is enabled. ({e})")


if __name__ == "__main__":
    main()
