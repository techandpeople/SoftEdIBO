* Hospital for Robots: Fixed Tuned 5V/2.5V Receiver
.param FREQ=13.56meg
.param RF_VAL=2.2k
.param CF_VAL=2.2p

*** POWER & VIRTUAL GROUND ***
Vcc vcc 0 5.0
Vbias v_half_ref 0 2.5

*** INPUT: Active Wristband + Body + 3mm Cylinder ***
Vtx tx_node 0 SINE(0 5 {FREQ})
Cwrist tx_node body_node 100p
Rbody body_node finger_node 500
Cfinger finger_node cylinder_node 1p

*** MUX508 PARASITICS ***
Rmux cylinder_node d_bus 125
Cmux d_bus 0 15p

*** STAGE 1: TIA (U4A) ***
Xop1 d_bus v_half_ref out_tia vcc 0 opamp_250m
Rf out_tia d_bus {RF_VAL}
Cf out_tia d_bus {CF_VAL}

*** STAGE 2: VOLTAGE AMP (U4B) ***
* FIXED: in_neg2 goes to in-, out_tia goes to in+
Xop2 in_neg2 out_tia out_b vcc 0 opamp_250m
R3 out_b in_neg2 1.2k
R4 in_neg2 v_half_ref 1k

*** ENVELOPE DETECTOR (BAT54S Dual Diode) ***
D1A out_b adc_pa1 D_BAT54
D1B adc_pa1 v_half_ref D_BAT54
C_env adc_pa1 0 100p
R_env adc_pa1 0 10k

*** MCU PROTECTION ***
D2 0 adc_pa1 D_ZENER3V3

*** MODELS ***
.model D_BAT54 D(Is=5e-6 Rs=0.5 N=1.1 Cjo=10p)
.model D_ZENER3V3 D(Is=1e-11 Rs=2 N=1 BV=3.3 IBV=1m)

* FIXED: G_ol is now 1 (Yields true 250MHz GBW)
.subckt opamp_250m in- in+ out vcc vee
E_diff diff 0 in+ in- 1
G_ol 0 out_int diff 0 1
R_pole out_int 0 100k
C_pole out_int 0 636p
E_buf out 0 VALUE={ MIN( MAX(V(out_int)+V(in+), V(vee)), V(vcc) ) }
.ends

*** SIMULATION CONTROL ***
.control
  set hcopypscolor=1
  set hcopyscale=2

  .ic V(v_half_ref)=2.5
  tran 1n 10u
  hardcopy plot.ps v(out_tia) v(out_b) xlimit 9.5u 10u
  hardcopy plot1.ps v(adc_pa1) xlimit 0 10u
.endc
.end
