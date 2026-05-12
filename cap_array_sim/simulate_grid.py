import os
import sys
import numpy as np

# NumPy 1.24+ Alias Patch
np.float = float

try:
    import CSXCAD
    import openEMS
except ImportError:
    print("FATAL: CSXCAD or openEMS not found.")
    sys.exit(1)

print("--- Hospital for Robots: Hardened FDTD Solver ---")

f0 = 13.56e6
FDTD = openEMS.openEMS()
FDTD.SetGaussExcite(f0, f0)
FDTD.SetBoundaryCond(["PML_8", "PML_8", "PML_8", "PML_8", "PML_8", "PML_8"])

CSX = CSXCAD.ContinuousStructure()
FDTD.SetCSX(CSX)

# Materials
pec = CSX.AddMetal("Copper")
pvc = CSX.AddMaterial("PVC", epsilon=2.2)
silicone = CSX.AddMaterial("Silicone", epsilon=3.0)

# Geometry (mm)
grid_size = 100.0
r_wire, r_case = 0.25, 0.5
z_y, z_x = 0.0, 15.0
z_sil, z_hand = 18.0, 19.0
coords = [12.5, 37.5, 62.5, 87.5]

for c in coords:
    pvc.AddCylinder(start=[0, c, z_y], stop=[grid_size, c, z_y], radius=r_case)
    pec.AddCylinder(start=[0, c, z_y], stop=[grid_size, c, z_y], radius=r_wire)
    pvc.AddCylinder(start=[c, 0, z_x], stop=[c, grid_size, z_x], radius=r_case)
    pec.AddCylinder(start=[c, 0, z_x], stop=[c, grid_size, z_x], radius=r_wire)

silicone.AddBox(start=[0, 0, z_x], stop=[grid_size, grid_size, z_sil])

# Port: Hand to X-Layer
port_start = [50, 50, z_hand]
port_stop = [50, 50, z_x]
port = FDTD.AddLumpedPort(
    port_nr=1, R=50.0, start=port_start, stop=port_stop, p_dir="z", excite=1
)
pec.AddBox(start=[30, 30, z_hand], stop=[70, 70, z_hand + 5])

# --- CRITICAL: HARDENED MESH ---
mesh = CSX.GetGrid()
mesh.SetDeltaUnit(1e-3)

# Build explicit mesh lines
x_lines = [0, 50, grid_size]
y_lines = [0, 50, grid_size]
z_lines = [-10, z_y, z_x, z_sil, z_hand, z_hand + 5, 40]

for c in coords:
    x_lines += [c - r_case, c, c + r_case]
    y_lines += [c - r_case, c, c + r_case]

# Final Sort/Unique to prevent non-monotonical error
mesh.AddLine("x", np.unique(np.sort(x_lines)))
mesh.AddLine("y", np.unique(np.sort(y_lines)))
mesh.AddLine("z", np.unique(np.sort(z_lines)))

# Create the simulation folder
output_dir = "fdtd_results"
if os.path.exists(output_dir):
    import shutil

    shutil.rmtree(output_dir)
os.mkdir(output_dir)

# 6. Run Engine
print("\n🚀 Executing OpenEMS Binary...")
CSX.Write2XML(os.path.join(output_dir, "sim.xml"))
FDTD.Run(output_dir)

# 7. Post-Processing
print("\n📊 Extracting Impedance...")
port.CalcPort(output_dir, np.array([f0]))
Z = port.uf_tot / port.if_tot
Z_target = np.atleast_1d(Z)[0]

C_farads = -1.0 / (2 * np.pi * f0 * np.imag(Z_target))

print("\n" + "=" * 40)
print(f"COUPLING C: {C_farads * 1e15:.2f} fF")
print("=" * 40)
