import numpy as np
import matplotlib.pyplot as plt

# --- System Constants ---
F_CARRIER = 13.56e6
OMEGA = 2 * np.pi * F_CARRIER
V_IN = 5.0
GAIN = 20.0
EPS0 = 8.854e-12

# Component Values
L = 1e-6  # 1uH
C_TANK = 120e-12  # 120pF
C_PARA = 5e-12  # MUX + Op-amp parasitics
R_BLEED = 10e6  # 10Mohm
R_SERIES = 0.5  # Inductor ESR (important for Q)


# --- Physics: Capacitance vs Distance ---
def get_capacitance(d_mm, area_cm2=1.0, t_silicone_mm=3.0):
    # Series: 3mm Silicone (er=3) + Air gap (d-3mm)
    area = area_cm2 * 1e-4
    t_s = t_silicone_mm * 1e-3
    t_a = max(0, (d_mm - t_silicone_mm) * 1e-3)
    return (EPS0 * area) / ((t_s / 3.0) + (t_a / 1.0))


# --- Impedance Logic ---
# Z_afe = L // C // R
z_l = 1j * OMEGA * L + R_SERIES
z_c_afe = 1 / (1j * OMEGA * (C_TANK + C_PARA))
z_afe = 1 / (1 / z_l + 1 / z_c_afe + 1 / R_BLEED)

distances = np.linspace(1, 50, 100)
v_out = []

for d in distances:
    c_coup = get_capacitance(d)
    z_coup = 1 / (1j * OMEGA * c_coup)

    # Voltage Divider
    v_sense = V_IN * np.abs(z_afe / (z_coup + z_afe))
    v_out.append(v_sense * GAIN)

# --- Plotting ---
plt.figure(figsize=(10, 6))
plt.plot(distances, v_out, linewidth=2, color="#2c3e50", label="AFE Response (Gain=20)")
plt.axhline(y=0.006, color="#e74c3c", linestyle="--", label="MX3501 Threshold (6mV)")

plt.yscale("log")
plt.xlabel("Distance (mm)")
plt.ylabel("Peak Voltage (V)")
plt.title("Analytical Model: 13.56MHz Capacitive Link Budget")
plt.grid(True, which="both", alpha=0.3)
plt.legend()
plt.show()
