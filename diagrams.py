#!/usr/bin/env python3
"""
Generate detailed explanatory diagrams for every part of the BMS algorithm.
Each diagram is self-contained and understandable by a non-technical reader.
"""
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Circle
from scipy.interpolate import PchipInterpolator
import os

OUT = "bms_plots"

def _box(ax, x, y, w, h, txt, fc, ec="#333", fs=9, ha="center", va="center"):
    ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle="round,pad=.05,rounding_size=.12",
                                fc=fc,ec=ec,lw=1.3))
    ax.text(x+w/2,y+h/2,txt,ha=ha,va=va,fontsize=fs,linespacing=1.3)

def _arr(ax, x1,y1,x2,y2,c="#444",s="-|>"):
    ax.add_patch(FancyArrowPatch((x1,y1),(x2,y2),arrowstyle=s,mutation_scale=14,lw=1.3,color=c))

# ══════════════════════════════════════════════════════════════
#  DIAGRAM 1: System Overview — What is this and why?
# ══════════════════════════════════════════════════════════════
def diagram_1_overview():
    fig,ax=plt.subplots(figsize=(16,9)); ax.axis("off"); ax.set_xlim(0,16); ax.set_ylim(0,9)

    # Title
    ax.text(8,8.5,"SYSTEM OVERVIEW: What does this BMS do?",ha="center",fontsize=14,weight="bold")
    ax.text(8,8.0,"A battery management system that figures out how full each cell is,\n"
            "even when the batteries arrive from an unknown supplier with no labels.",
            ha="center",fontsize=10,color="#555")

    # Battery pack
    _box(ax,0.3,4,2.5,2.5,"[BATT] 20 LFP Cells\n\nCharge level: unknown\nHealth: varies, unknown\n(estimated separately)\n\n(from dodgy supplier)","#f5f5f5",fs=9)
    ax.text(1.55,3.7,"THE PROBLEM",ha="center",fontsize=8,weight="bold",color="#c00")

    # Sensors
    _box(ax,3.5,5.8,2.2,0.8,"[V] Voltage sensor\n(measures 3.28 ± 0.003 V)","#dce8fb",fs=8.5)
    _box(ax,3.5,4.5,2.2,0.8,"[I] Current sensor\n(measures pack current)","#dce8fb",fs=8.5)
    _box(ax,3.5,3.2,2.2,0.8,"[F] Force sensor (per cell)\n(measures expansion pressure)","#dcefe0",fs=8.5)
    for y in (6.2,4.9,3.6): _arr(ax,2.8,5.25,3.5,y)

    # Brain
    _box(ax,6.5,3.5,3.5,3.5,
         "[BRAIN] KALMAN FILTER\n(the brain)\n\n"
         "Every second, it:\n"
         "1. Predicts where SOC should be\n"
         "2. Compares prediction to sensors\n"
         "3. Corrects its estimate\n"
         "4. Repeats forever\n\n"
         "Two versions compared, both estimating\n"
         "SOC + resistance from voltage:\n"
         "• Baseline (voltage + current only)\n"
         "• Enhanced (+ force + ICA signals)","#fff8e0",fs=9)
    for y in (6.2,4.9,3.6): _arr(ax,5.7,y,6.5,5.25)

    # Outputs
    _box(ax,10.8,5,4,2,
         "[OK] OUTPUTS\n\n"
         "• Pack SOC (how full overall)\n"
         "• Per-cell SOC (each cell)\n"
         "• Internal resistance\n"
         "(SOH: a separate BMS method,\n"
         " out of scope here)\n\n"
         "Goal: < 5% error in 20 min","#e3f7e8",fs=9)
    _arr(ax,10,5.25,10.8,6)

    # Why it's hard
    _box(ax,10.8,2,4,2.3,
         "[!!] WHY THIS IS HARD\n\n"
         "LFP batteries have a FLAT\n"
         "voltage curve — the voltage\n"
         "barely changes from 20% to\n"
         "80% full. It's like trying to\n"
         "read a fuel gauge that's stuck\n"
         "in the middle.","#fde8e8",fs=9)

    fig.tight_layout()
    fig.savefig(f"{OUT}/diagram_1_overview.png",dpi=140); plt.close(fig)

# ══════════════════════════════════════════════════════════════
#  DIAGRAM 2: The LFP Plateau Problem
# ══════════════════════════════════════════════════════════════
def diagram_2_plateau():
    _Z=np.array([0,0.02,0.04,0.06,0.08,0.10,0.13,0.16,0.20,0.25,0.30,0.35,0.40,0.45,0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85,0.88,0.92,0.95,0.97,0.99,1.0])
    _V=np.array([2.5,2.72,2.87,2.96,3.05,3.15,3.22,3.25,3.265,3.272,3.276,3.278,3.28,3.281,3.282,3.283,3.284,3.285,3.287,3.29,3.294,3.30,3.308,3.32,3.34,3.36,3.42,3.6])
    fn=PchipInterpolator(_Z,_V)

    fig,axes=plt.subplots(1,2,figsize=(16,7))

    # Left: the voltage curve
    zz=np.linspace(0,1,500); vv=fn(zz)
    axes[0].plot(zz*100,vv,'b-',lw=3)
    axes[0].axvspan(15,85,color='#ffe0e0',alpha=.4)
    axes[0].annotate("DANGER ZONE\nVoltage barely changes\n(57 mV across 70% of SOC range)\n→ SOC estimate is BLIND here",
                     xy=(50,3.283),xytext=(55,3.40),fontsize=10,weight='bold',color='#c00',
                     arrowprops=dict(arrowstyle='->',color='#c00',lw=2),ha='center')
    axes[0].annotate("Steep tail\n→ EKF works here",xy=(5,2.9),xytext=(15,2.7),fontsize=9,
                     arrowprops=dict(arrowstyle='->',color='green',lw=1.5))
    axes[0].annotate("Steep tail\n→ EKF works here",xy=(97,3.5),xytext=(85,3.50),fontsize=9,
                     arrowprops=dict(arrowstyle='->',color='green',lw=1.5))
    axes[0].set(title="LFP Open-Circuit Voltage vs State of Charge",
                xlabel="SOC [%]",ylabel="Voltage [V]")
    # Zoom inset
    axins=axes[0].inset_axes([0.15,0.55,0.55,0.35])
    axins.plot(zz*100,vv,'b-',lw=2)
    axins.set_xlim(20,80); axins.set_ylim(3.270,3.300)
    axins.set_title("ZOOMED: 20%–80% SOC",fontsize=8)
    axins.axhline(3.282,color='r',ls=':',lw=1)
    axins.axhline(3.294,color='r',ls=':',lw=1)
    axins.annotate("",xy=(75,3.282),xytext=(75,3.294),arrowprops=dict(arrowstyle='<->',color='r',lw=2))
    axins.text(76,3.288,"12 mV\ntotal!",fontsize=8,color='r',weight='bold')

    # Right: the analogy
    axes[1].axis('off')
    axes[1].text(0.5,0.95,"WHY THIS MATTERS",ha='center',fontsize=14,weight='bold',transform=axes[1].transAxes)
    explanations = [
        ("Imagine a fuel gauge...",0.85,"#333",11),
        ("For a NORMAL car (NMC battery):\n"
         "  The needle moves smoothly from E to F\n"
         "  → You always know how much fuel you have",0.70,"#060",10),
        ("For an LFP battery:\n"
         "  The needle is STUCK in the middle\n"
         "  from 15% to 85% full\n"
         "  → You can't tell if you have\n"
         "     20% or 70% remaining!",0.48,"#c00",10),
        ("SOLUTION: Add a PRESSURE GAUGE\n"
         "  LFP cells expand as they charge.\n"
         "  The expansion IS different at 20% vs 70%.\n"
         "  → Force sensor gives info where voltage can't.",0.22,"#006",10),
        ("That's what the Enhanced EKF does:\n"
         "  it combines voltage + force + current\n"
         "  to estimate SOC even in the flat zone.",0.05,"#333",10),
    ]
    for txt,y,c,fs in explanations:
        axes[1].text(0.05,y,txt,fontsize=fs,color=c,transform=axes[1].transAxes,
                     va='top',family='monospace',linespacing=1.4)

    fig.suptitle("DIAGRAM 2: The LFP Voltage Plateau Problem",fontsize=13,weight='bold')
    fig.tight_layout(); fig.savefig(f"{OUT}/diagram_2_plateau.png",dpi=140); plt.close(fig)

# ══════════════════════════════════════════════════════════════
#  DIAGRAM 3: Baseline EKF — step by step
# ══════════════════════════════════════════════════════════════
def diagram_3_classical():
    fig,ax=plt.subplots(figsize=(16,10)); ax.axis("off"); ax.set_xlim(0,16); ax.set_ylim(0,10)
    ax.text(8,9.5,"DIAGRAM 3: Baseline EKF — Step by Step",ha="center",fontsize=14,weight="bold")
    ax.text(8,9.0,"This runs every second. It has TWO states: z (SOC) and R₀ (internal resistance) —\n"
            "estimated jointly, so it is a fair voltage-only baseline against the Enhanced EKF.",
            ha="center",fontsize=10,color="#555")

    # Step 1
    _box(ax,0.3,6.5,3.5,2,
         "STEP 1: PREDICT\n\n"
         "z_new = z_old + I × Δt / C_nom\n"
         "R₀_new = R₀_old  (near-constant)\n\n"
         "\"If the current is 280A for 1 sec,\n"
         " the SOC should increase by\n"
         " 280/(3600×280) = 0.028%\"","#dce8fb",fs=9)
    ax.text(1.3,6.2,"Coulomb counting",fontsize=8,style='italic',color='#555')

    # Arrow
    _arr(ax,3.8,7.5,4.5,7.5)

    # Step 2
    _box(ax,4.5,6.5,3.5,2,
         "STEP 2: MEASURE\n\n"
         "Read voltage from sensor: V_meas\n\n"
         "Calculate what voltage SHOULD be,\n"
         "using the CURRENT R₀ ESTIMATE:\n"
         "V_pred = OCV(z_pred) + I × R₀_pred\n\n"
         "Innovation = V_meas - V_pred","#fff8e0",fs=9)

    _arr(ax,8,7.5,8.7,7.5)

    # Step 3
    _box(ax,8.7,6.5,3.5,2,
         "STEP 3: COMPARE\n\n"
         "H = [dOCV/dz , I]  (2 entries)\n\n"
         "dOCV/dz SMALL (LFP plateau):\n"
         "  → voltage says little about z\n\n"
         "I large & varying:\n"
         "  → voltage says a lot about R₀\n"
         "  → residual gets attributed there,\n"
         "     not forced onto z","#fde8e8",fs=8.5)

    _arr(ax,12.2,7.5,12.9,7.5)

    # Step 4
    _box(ax,12.5,6.5,3,2,
         "STEP 4: UPDATE\n\n"
         "K = P·Hᵀ / (H·P·Hᵀ + R)\n\n"
         "[z; R₀] += K × innov.\n\n"
         "z-gain ≈ 0 in plateau,\n"
         "R₀-gain absorbs the\n"
         "resistance mismatch","#fbe3e6",fs=9)

    # Loop back arrow
    ax.annotate("",xy=(1.5,6.5),xytext=(14,6.5),
                arrowprops=dict(arrowstyle='-|>',color='#888',lw=2,
                connectionstyle="arc3,rad=0.4"))
    ax.text(8,5.4,"⟲ REPEAT EVERY SECOND",ha='center',fontsize=11,color='#888',weight='bold')

    # Problem box
    _box(ax,2,1,12,3.5,
         "[!!]  WHAT STILL LIMITS THE BASELINE EKF ON LFP\n\n"
         "Adding R₀ as a second state removes the OLD failure mode: a fixed, wrong resistance\n"
         "guess no longer gets misattributed onto SOC (that mechanism alone used to blow up the\n"
         "error to 20-30%, because the plateau slope H≈0.02-0.04 V/unit-SOC amplifies any IR\n"
         "misattribution). With R₀ estimated online, that source of error largely disappears —\n"
         "error settles back to roughly what plain coulomb counting achieves on its own (a few\n"
         "percent, limited by current-sensor bias), which is the textbook EKF result people expect.\n\n"
         "What remains: in the plateau, z's own Kalman gain is still ≈0, so if current is SMOOTH\n"
         "and rarely reverses, R₀ and z can become hard to tell apart (poor observability) and the\n"
         "estimate can drift for longer between corrections. This is precisely the situation where\n"
         "an independent signal — force or ICA — can add real, non-redundant information.","#fff0f0",fs=9.5)

    fig.tight_layout(); fig.savefig(f"{OUT}/diagram_3_classical_ekf.png",dpi=140); plt.close(fig)

# ══════════════════════════════════════════════════════════════
#  DIAGRAM 4: Enhanced EKF — step by step
# ══════════════════════════════════════════════════════════════
def diagram_4_enhanced():
    fig,ax=plt.subplots(figsize=(16,12)); ax.axis("off"); ax.set_xlim(0,16); ax.set_ylim(0,12)
    ax.text(8,11.5,"DIAGRAM 4: Enhanced 3-State EKF — Step by Step",ha="center",fontsize=14,weight="bold")
    ax.text(8,11.0,"Three states: z (SOC), R₀ (resistance), f_bias (force offset)",
            ha="center",fontsize=10,color="#555")

    # Predict
    _box(ax,0.3,8.5,4.5,2.2,
         "STEP 1: PREDICT\n\n"
         "z = z + I × Δt / (3600 × C_nom)\n"
         "R₀ = R₀ (unchanged)\n"
         "f_bias = f_bias (unchanged)\n\n"
         "★ Same nameplate capacity as the\n"
         "  baseline filter -- SOH estimation\n"
         "  is a separate BMS method, not\n"
         "  part of this SOC estimator.","#dce8fb",fs=9)

    _arr(ax,4.8,9.6,5.5,9.6)

    # Voltage update
    _box(ax,5.5,8.5,5,2.2,
         "STEP 2a: VOLTAGE UPDATE\n\n"
         "Prediction: V_pred = OCV(z) + I × R₀\n"
         "Innovation: y = V_meas - V_pred\n"
         "Sensitivity: H = [dOCV/dz, I, 0]\n\n"
         "★ R_U is ADAPTIVE (Jia tanh rule):\n"
         "  If voltage prediction is bad → trust V less\n"
         "  If voltage prediction is good → trust V more","#fff8e0",fs=8.5)

    _arr(ax,10.5,9.6,11.2,9.6)

    # Tanh
    _box(ax,11.2,8.5,4.3,2.2,
         "TANH ARBITRATION\n(Jia et al. Eq.13-14)\n\n"
         "e = voltage prediction error\n"
         "R_U = R_max − ΔR × tanh(μ × e²)\n"
         "Q_F = Q_max − ΔQ × tanh(μ × e²)\n\n"
         "Large error → inflate R_U (trust V less)\n"
         "             → reduce Q_F (trust F more)\n"
         "Small error → vice versa","#fff2d9",fs=8.5)

    # Force update
    _box(ax,0.3,5.5,5.5,2.2,
         "STEP 2b: FORCE UPDATE\n\n"
         "Prediction: F_pred = f_bias + F_nominal(z)\n"
         "Innovation: y = F_meas - F_pred\n"
         "Sensitivity: H = [dF/dz, 0, 1]\n\n"
         "★ INFLECTION GATE: where |dF/dz| is small\n"
         "  (force curve is non-monotonic!), the filter\n"
         "  reduces the force sensor weight to avoid\n"
         "  converging to the WRONG SOC.","#dcefe0",fs=8.5)

    _arr(ax,5.8,6.6,6.5,6.6)

    # ICA
    _box(ax,6.5,5.5,5,2.2,
         "STEP 2c: ICA ANCHOR (rare, C/6 only)\n\n"
         "During slow charging (C/6), compute:\n"
         "  dQ/dV = ΔCharge / ΔVoltage\n\n"
         "When dQ/dV peaks → we know the SOC\n"
         "exactly (it corresponds to a graphite\n"
         "phase transition at a known SOC).\n\n"
         "★ Per Fly & Chen: only works at ≤C/6.\n"
         "  At 1C, peaks merge and disappear.","#f0e8ff",fs=8.5)

    _arr(ax,11.5,6.6,12,6.6)

    # Output
    _box(ax,11.5,5.5,4,2.2,
         "STEP 3: OUTPUT\n\n"
         "After all updates, we have:\n"
         "• z → SOC per cell\n"
         "• R₀ → internal resistance\n"
         "• f_bias → force calibration\n\n"
         "★ z converges in ~15-25 min\n"
         "  (SOH: separate method, n/a here)","#e3f7e8",fs=8.5)

    # Loop
    ax.annotate("",xy=(1.5,8.5),xytext=(13.5,5.5),
                arrowprops=dict(arrowstyle='-|>',color='#888',lw=2,connectionstyle="arc3,rad=0.5"))
    ax.text(0.5,4.8,"⟲ REPEAT",fontsize=11,color='#888',weight='bold')

    # Key insight
    _box(ax,0.3,1,15.2,2.8,
         "[*] KEY INSIGHT: what force+ICA actually adds, once the comparison is fair\n\n"
         "Both filters here estimate R₀ online, so both already close the OLD, large (20-30%) drift caused by\n"
         "a wrong fixed resistance guess. What's left for force/ICA to contribute is narrower: extra SOC\n"
         "information in the plateau when current is smooth and R₀ is hard to separate from z from V+I alone.\n\n"
         "The FORCE sensor has a clear slope (dF/dz ≈ 22 N per unit SOC) through most of the plateau, and the\n"
         "tanh arbitration lets the filter lean on it when voltage tracking looks poor. Capacity/SOH is\n"
         "deliberately NOT a state here -- both filters coulomb-count against the same nameplate capacity,\n"
         "so any remaining gap between them comes only from the voltage/force/ICA channels, not resistance.","#f8f8ff",fs=9.5)

    fig.tight_layout(); fig.savefig(f"{OUT}/diagram_4_enhanced_ekf.png",dpi=140); plt.close(fig)

# ══════════════════════════════════════════════════════════════
#  DIAGRAM 5: Force sensor — why it helps
# ══════════════════════════════════════════════════════════════
def diagram_5_force():
    zz=np.linspace(0,1,500)
    fv=22*(zz-0.5)-10*np.sin(2*np.pi*(zz-0.5))
    df=22-10*2*np.pi*np.cos(2*np.pi*(zz-0.5))

    fig,axes=plt.subplots(1,3,figsize=(17,6))

    # Force curve
    axes[0].plot(zz*100,fv,'m-',lw=2.5)
    axes[0].axvspan(15,85,color='#ffe0e0',alpha=.3,label='voltage plateau')
    axes[0].set(title="Force vs SOC\n(expansion pressure on the cell)",xlabel="SOC [%]",ylabel="Force [N] (no preload)")
    axes[0].annotate("Monotonic here\n→ force gives\nclear SOC info",xy=(20,-5),xytext=(10,-8),
                     fontsize=9,arrowprops=dict(arrowstyle='->',color='green'),color='green')
    axes[0].annotate("Inflection!\n→ force can mislead\n→ gate reduces weight",xy=(30,fv[150]),xytext=(40,-6),
                     fontsize=9,arrowprops=dict(arrowstyle='->',color='red'),color='red')
    axes[0].legend(fontsize=8)

    # dF/dz (sensitivity)
    axes[1].plot(zz*100,np.abs(df),'g-',lw=2)
    axes[1].axhline(0,color='k',lw=.5)
    axes[1].axvspan(15,85,color='#ffe0e0',alpha=.3)
    axes[1].fill_between(zz*100,0,np.abs(df),where=np.abs(df)<5,color='red',alpha=.3,label='inflection zones (gate active)')
    axes[1].set(title="|dF/dz| — Force sensitivity\nHigher = more SOC information",xlabel="SOC [%]",ylabel="|dF/dz| [N per unit SOC]")
    axes[1].legend(fontsize=8)

    # Comparison: voltage vs force info
    _Z=np.array([0,0.02,0.04,0.06,0.08,0.10,0.13,0.16,0.20,0.25,0.30,0.35,0.40,0.45,0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85,0.88,0.92,0.95,0.97,0.99,1.0])
    _V=np.array([2.5,2.72,2.87,2.96,3.05,3.15,3.22,3.25,3.265,3.272,3.276,3.278,3.28,3.281,3.282,3.283,3.284,3.285,3.287,3.29,3.294,3.30,3.308,3.32,3.34,3.36,3.42,3.6])
    docv=PchipInterpolator(_Z,_V).derivative()
    dv=np.abs(docv(zz))
    # Normalize both to [0,1] for comparison
    dv_n=np.clip(dv/dv.max(),0,1)
    df_n=np.clip(np.abs(df)/np.abs(df).max(),0,1)
    axes[2].fill_between(zz*100,0,dv_n,alpha=.4,color='red',label='Voltage info')
    axes[2].fill_between(zz*100,0,df_n,alpha=.4,color='blue',label='Force info')
    axes[2].axvspan(15,85,color='#ffe0e0',alpha=.15)
    axes[2].set(title="Voltage vs Force information\n(normalized)",xlabel="SOC [%]",ylabel="Relative info content")
    axes[2].legend(fontsize=9)
    axes[2].annotate("Force fills the gap\nwhere voltage is blind!",xy=(50,0.7),fontsize=10,weight='bold',color='#006',ha='center')

    fig.suptitle("DIAGRAM 5: How Force Sensing Solves the LFP Problem",fontsize=13,weight='bold')
    fig.tight_layout(); fig.savefig(f"{OUT}/diagram_5_force.png",dpi=140); plt.close(fig)

# ══════════════════════════════════════════════════════════════
#  DIAGRAM 6: Scenarios explained
# ══════════════════════════════════════════════════════════════
def diagram_6_scenarios():
    fig,axes=plt.subplots(3,1,figsize=(15,10),sharex=False)
    C1=280

    # Peak Shaving
    t1=np.arange(7200); I1=np.zeros(7200)
    for k in range(7200):
        s=k
        if s<900: I1[k]={True:0,60<=s<120:0.5*C1,120<=s<180:0,180<=s<420:-0.4*C1,420<=s<480:0}.get(True,C1/6)
        elif s<2400: I1[k]=-0.85*C1
        elif s<2700: I1[k]=0
        elif s<5400: I1[k]=0.60*C1
        elif s<6000: I1[k]=-0.35*C1
        else: I1[k]=0.25*C1
    # Simpler version
    t1=np.linspace(0,120,7200)
    I1=np.zeros(7200)
    for k in range(7200):
        m=t1[k]
        if m<15: I1[k]=0.3*C1 if m>1 else 0
        elif m<40: I1[k]=-0.85*C1
        elif m<45: I1[k]=0
        elif m<90: I1[k]=0.6*C1
        elif m<105: I1[k]=-0.35*C1
        else: I1[k]=0.25*C1
    axes[0].plot(t1,I1/C1,'g-',lw=1); axes[0].axhline(0,color='k',lw=.5)
    axes[0].fill_between(t1,0,I1/C1,where=I1>0,alpha=.15,color='green',label='Charging')
    axes[0].fill_between(t1,0,I1/C1,where=I1<0,alpha=.15,color='red',label='Discharging')
    axes[0].set(title="Scenario 1: PEAK SHAVING (2 hours)\nBESS charges at night, discharges during peak demand",
                ylabel="C-rate",ylim=(-1.1,1.1)); axes[0].legend(fontsize=8,ncol=2)

    # PV
    t2=np.linspace(0,120,7200); I2=np.zeros(7200)
    for k in range(7200):
        m=t2[k]
        if m<15: I2[k]=0.3*C1 if m>1 else 0
        elif m<75:
            s=(m-15)/60; solar=np.exp(-((s-0.5)/0.25)**2)*0.7
            I2[k]=(solar-0.15)*C1
        else: I2[k]=-0.3*C1
    axes[1].plot(t2,I2/C1,'g-',lw=1); axes[1].axhline(0,color='k',lw=.5)
    axes[1].fill_between(t2,0,I2/C1,where=I2>0,alpha=.15,color='orange',label='PV charging')
    axes[1].fill_between(t2,0,I2/C1,where=I2<0,alpha=.15,color='blue',label='Evening load')
    axes[1].set(title="Scenario 2: PV SELF-CONSUMPTION (2 hours)\nSolar charges battery during day, evening discharge",
                ylabel="C-rate",ylim=(-1.1,1.1)); axes[1].legend(fontsize=8,ncol=2)

    # FCR - realistic: mostly idle with occasional response + recovery
    t3=np.linspace(0,120,7200); I3=np.zeros(7200)
    frng=np.random.default_rng(12345)
    # OU frequency wander
    df=np.zeros(7200); theta,sigma=0.05,0.004
    nz=frng.standard_normal(7200)
    for i in range(1,7200):
        df[i]=df[i-1]-theta*df[i-1]*1+sigma*np.sqrt(1)*nz[i]
    for c,a,w in [(45,0.09,3),(96,-0.12,4),(75,0.07,2.5)]:
        df+=a*np.exp(-((t3-c)/w)**2)
    # convert to current: deadband + droop
    for k in range(7200):
        if t3[k]<15: I3[k]=0.3 if t3[k]>1 else 0; continue
        mag=min(max(0,(abs(df[k])-0.010)/(0.200-0.010)),1)
        I3[k]=np.sign(df[k])*mag
    axes[2].plot(t3,I3,'g-',lw=.6); axes[2].axhline(0,color='k',lw=.5)
    axes[2].fill_between(t3,0,I3,where=I3>0,alpha=.15,color='green')
    axes[2].fill_between(t3,0,I3,where=I3<0,alpha=.15,color='red')
    axes[2].set(title="Scenario 3: FCR / FREQUENCY REGULATION (2 hours)\nMostly idle inside deadband; small droop response + SOC recovery on grid events",
                ylabel="C-rate",xlabel="time [min]",ylim=(-1.1,1.1))

    for ax in axes:
        ax.axvspan(0,15,color='#eef3fb',alpha=.5); ax.text(7.5,0.95,'commissioning',fontsize=7,ha='center',color='#555')

    fig.suptitle("DIAGRAM 6: The Three Scenarios",fontsize=13,weight='bold')
    fig.tight_layout(); fig.savefig(f"{OUT}/diagram_6_scenarios.png",dpi=140); plt.close(fig)


if __name__=="__main__":
    os.makedirs(OUT,exist_ok=True)
    diagram_1_overview()
    diagram_2_plateau()
    diagram_3_classical()
    diagram_4_enhanced()
    diagram_5_force()
    diagram_6_scenarios()
    print(f"6 diagrams saved to {OUT}/")
