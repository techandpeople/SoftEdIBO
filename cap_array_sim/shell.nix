{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    ngspice
    klayout
    kicad
    python311
    uv
    podman
  ];

  shellHook = ''
    echo "--- Hospital for Robots: Offline-First Environment ---"
    if [ ! -d ".venv" ]; then
      uv venv
    fi
    source .venv/bin/activate
  '';
}
