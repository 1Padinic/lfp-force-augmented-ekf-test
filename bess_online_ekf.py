#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Online BMS for 20 LFP pouch cells (280 Ah) — unknown SOC (SOH out of scope).
Force+ICA augmented EKF vs a fair voltage-only baseline EKF.

Both filters estimate [z, R0] (SOC + internal resistance) from voltage and
coulomb counting -- this is deliberate, so that a win for the enhanced
filter isolates what the force+ICA channels contribute, rather than being
confounded with "it also gets to estimate resistance and the baseline
doesn't." The enhanced filter additionally estimates a 3rd state, f_bias
(force-sensor offset), and adds two more measurement channels on top of the
shared [z, R0] voltage update:
  • Force: non-monotonic, inflections per Jia et al. (2024 IEEE ITEC)
  • ICA: only at ≤C/6 per Fly & Chen (2020 J. Energy Storage)
  • tanh arbitration: Jia Eq. 13-14 noise covariance adaptive update

Three duty-cycle scenarios: Peak Shaving, PV Self-Consumption, FCR (2h each).

Anti-cheating (no inverse crime):
  • Truth uses unmodelled 1RC diffusion + per-cell OCV offset/tilt
  • Per-cell force amplitude/phase perturbation
  • Persistent current sensor gain+offset bias
  • PyBaMM Prada2013 SPMe for 2 cells (if available)
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.interpolate import PchipInterpolator
import os, warnings
warnings.filterwarnings("ignore")

try:
    import pybamm
    HAS_PYBAMM = True
except ImportError:
    HAS_PYBAMM = False

# =====================================================================
#  CONFIG
# =====================================================================
N_CELLS  = 20
C_NOM    = 280.0          # Ah nameplate
DT       = 1.0            # s
SIM_MIN  = 120             # per scenario (2 hours)
T_STEPS  = int(SIM_MIN * 60 / DT)

SIG_V = 0.003             # 3 mV
SIG_F = 3.0               # 3 N  — realistic load cell on a module
SIG_I = 0.5               # 0.5 A per-sample (on top of bias)

# =====================================================================
#  1.  LFP OCV — matched to CATL 280 Ah prismatic cells
#      Plateau covers SOC 0.15–0.85 with ~40 mV variation.
# =====================================================================
_Z_OCV = np.array([
    0.000, 0.020, 0.040, 0.060, 0.080, 0.100,
    0.130, 0.160, 0.200, 0.250, 0.300, 0.350,
    0.400, 0.450, 0.500, 0.550, 0.600, 0.650,
    0.700, 0.750, 0.800, 0.850, 0.880, 0.920,
    0.950, 0.970, 0.990, 1.000])
_V_OCV = np.array([
    2.500, 2.720, 2.870, 2.960, 3.050, 3.150,
    3.220, 3.250, 3.265, 3.272, 3.276, 3.278,
    3.280, 3.281, 3.282, 3.283, 3.284, 3.285,
    3.287, 3.290, 3.294, 3.300, 3.308, 3.320,
    3.340, 3.360, 3.420, 3.600])
_ocv_fn  = PchipInterpolator(_Z_OCV, _V_OCV)
_docv_fn = _ocv_fn.derivative()

def ocv_nom(z):    return _ocv_fn(np.clip(z, 0.0, 1.0))
def docv_dz_nom(z): return _docv_fn(np.clip(z, 0.0, 1.0))

# Force: non-monotonic per Jia et al.
_F_LIN, _F_SIN = 22.0, 10.0
def force_nom(z):
    z = np.clip(z, 0.0, 1.0)
    return _F_LIN * (z - 0.5) - _F_SIN * np.sin(2*np.pi*(z - 0.5))
def dforce_dz_nom(z):
    z = np.clip(z, 0.0, 1.0)
    return _F_LIN - _F_SIN * 2*np.pi * np.cos(2*np.pi*(z - 0.5))

# ICA peak SOC (flattest point)
_zz = np.linspace(0.15, 0.80, 2000)
ICA_PEAK_SOC = float(_zz[np.argmin(np.abs(docv_dz_nom(_zz)))])

# =====================================================================
#  2.  PACK GENERATION
# =====================================================================
def make_per_cell_ocv(rng, n):
    ocvs = []
    for _ in range(n):
        v_off  = rng.normal(0, 3.0e-3)
        v_tilt = rng.normal(0, 4.0e-3)
        v_pert = _V_OCV + v_off + v_tilt * (_Z_OCV - 0.5)
        ocvs.append(PchipInterpolator(_Z_OCV, v_pert))
    return ocvs

# Pack's normal operating band (matches the hard clip [0.10, 0.80] applied to
# z_true in simulate(), with margin so cells don't start already at the wall).
Z0_LO, Z0_HI = 0.15, 0.75

def sample_init_socs(rng, soc_scenario=None):
    """
    Pseudo-random initial-SOC assignment across the pack (dodgy supplier ->
    cells can arrive anywhere in the operating band, sometimes clustered).

    None (baseline): all N_CELLS uniform random in [Z0_LO, Z0_HI] -- the
                      original single-mode sampling.
    'A': 13 cells in [0.20, 0.35], remaining 7 uniform random in [Z0_LO, Z0_HI]
    'B': 5 cells in [0.10, 0.15], 5 cells in [0.25, 0.35],
         remaining 10 uniform random in [Z0_LO, Z0_HI]
    """
    if soc_scenario is None:
        return rng.uniform(Z0_LO, Z0_HI, N_CELLS)
    idx = rng.permutation(N_CELLS)
    socs = np.zeros(N_CELLS)
    if soc_scenario == 'A':
        cl, rest = idx[:13], idx[13:]
        socs[cl]   = rng.uniform(0.20, 0.35, len(cl))
        socs[rest] = rng.uniform(Z0_LO, Z0_HI, len(rest))
    elif soc_scenario == 'B':
        g1, g2, rest = idx[:5], idx[5:10], idx[10:]
        socs[g1]   = rng.uniform(0.10, 0.15, len(g1))
        socs[g2]   = rng.uniform(0.25, 0.35, len(g2))
        socs[rest] = rng.uniform(Z0_LO, Z0_HI, len(rest))
    else:
        raise ValueError(f"Unknown soc_scenario {soc_scenario!r}")
    return socs

def sample_pack(rng, soc_scenario=None):
    p = {}
    p["soh"]     = rng.uniform(0.80, 1.00, N_CELLS)   # true SOH spread (dodgy supplier).
    p["C"]       = C_NOM * p["soh"]                   # -> true per-cell capacity, ground truth only.
    p["z0"]      = sample_init_socs(rng, soc_scenario)
    p["R0"]      = rng.uniform(0.3e-3, 1.5e-3, N_CELLS)
    p["Rp"]      = rng.uniform(0.1e-3, 0.4e-3, N_CELLS)      # unmodelled diffusion
    p["tau"]     = rng.uniform(15.0, 55.0, N_CELLS)
    p["preload"] = rng.uniform(180.0, 220.0, N_CELLS)
    p["f_lin"]   = rng.uniform(0.80, 1.20, N_CELLS)           # per-cell force shape
    p["f_sin"]   = rng.uniform(0.75, 1.25, N_CELLS)
    p["f_phase"] = rng.uniform(-0.15, 0.15, N_CELLS)
    p["ocv_fns"] = make_per_cell_ocv(rng, N_CELLS)
    p["i_gain"]  = 1.0 + rng.normal(0, 0.006)                 # persistent bias
    p["i_off"]   = rng.normal(0, 0.6)
    return p

def force_true(z, i, p):
    z = np.clip(z, 0.0, 1.0)
    return (p["preload"][i]
            + _F_LIN * p["f_lin"][i] * (z - 0.5)
            - _F_SIN * p["f_sin"][i] * np.sin(2*np.pi*(z - 0.5) + p["f_phase"][i]))

# =====================================================================
#  3.  PYBAMM CELLS (Prada2013 LFP, if available)
# =====================================================================
def build_pybamm_cells(I_cmd, rng):
    if not HAS_PYBAMM:
        return []
    param_base = pybamm.ParameterValues('Prada2013')
    cap_prada  = param_base['Nominal cell capacity [A.h]']
    scale      = cap_prada / C_NOM
    t_eval     = np.arange(T_STEPS, dtype=float) * DT
    I_pybamm   = -I_cmd * scale           # BMS(+charge) → PyBaMM(+discharge)

    cells = []
    for ci in range(min(2, N_CELLS)):
        init_soc = float(rng.uniform(0.30, 0.65))
        p = param_base.copy()
        p.update({'Current function [A]': pybamm.Interpolant(t_eval, I_pybamm, pybamm.t)})
        try:
            sim = pybamm.Simulation(pybamm.lithium_ion.SPMe(), parameter_values=p)
            sol = sim.solve(t_eval=t_eval, initial_soc=init_soc)
            t_s = sol['Time [s]'].entries
            V_s = sol['Terminal voltage [V]'].entries
            I_a = sol['Current [A]'].entries
            Q_d = np.cumsum(np.gradient(t_s) * I_a) / 3600.0
            soc = init_soc - Q_d / cap_prada
            cells.append(dict(
                V=np.interp(t_eval, t_s, V_s),
                soc=np.interp(t_eval, t_s, soc),
                init_soc=init_soc))
        except Exception as e:
            print(f"  PyBaMM cell {ci} failed: {e}")
            cells.append(None)
    return cells

# =====================================================================
#  4.  CURRENT PROFILES
# =====================================================================
def profile_peak_shaving():
    """120-min peak shaving."""
    t = np.arange(T_STEPS) * DT
    I = np.zeros(T_STEPS); ph = np.empty(T_STEPS, dtype='<U15')
    ica = np.zeros(T_STEPS, dtype=bool); C1 = C_NOM
    for k in range(T_STEPS):
        s = t[k]
        if s < 900:
            ph[k] = "commission"
            if s<60: I[k]=0
            elif s<120: I[k]=+0.5*C1
            elif s<180: I[k]=0
            elif s<420: I[k]=-0.4*C1
            elif s<480: I[k]=0
            else: I[k]=+C1/6; ica[k]=True
        elif s<2400: ph[k]="eve_peak";  I[k]=-0.85*C1
        elif s<2700: ph[k]="rest";      I[k]=0
        elif s<5400: ph[k]="recharge";  I[k]=+0.60*C1
        elif s<6000: ph[k]="morn_peak"; I[k]=-0.35*C1
        else:        ph[k]="top_up";    I[k]=+0.25*C1
    return t, I, ph, ica

def profile_pv():
    """120-min PV self-consumption."""
    t = np.arange(T_STEPS) * DT
    I = np.zeros(T_STEPS); ph = np.empty(T_STEPS, dtype='<U15')
    ica = np.zeros(T_STEPS, dtype=bool); C1 = C_NOM
    for k in range(T_STEPS):
        s = t[k]
        if s < 900:
            ph[k] = "commission"
            if s<60: I[k]=0
            elif s<120: I[k]=+0.5*C1
            elif s<180: I[k]=0
            elif s<420: I[k]=-0.35*C1
            elif s<480: I[k]=0
            else: I[k]=+C1/6; ica[k]=True
        elif s<5400:
            ph[k] = "pv_gen"
            ss = s - 900
            mid = 2250.0; wid = 900.0
            solar = np.exp(-((ss-mid)/wid)**2) * 0.70
            cloud = 0.12*np.sin(2*np.pi*ss/50)
            load  = 0.12 + 0.05*np.sin(2*np.pi*ss/150)
            I[k] = (max(0, solar+cloud) - load) * C1
        else:
            ph[k] = "evening"
            ss = s - 5400
            I[k] = -(0.25 + 0.12*np.sin(2*np.pi*ss/100)) * C1
    return t, I, ph, ica

def profile_fcr():
    """120-min FCR — realistic: mostly idle inside the deadband, small droop
    response to grid-frequency deviations, with SOC recovery.

    This scenario is CLOSED-LOOP: the commanded power depends on the pack's
    (true) SOC through the recovery controller, so the current cannot be
    precomputed. The 'I' slot instead carries a synthetic grid-frequency
    deviation Δf [Hz] for the FCR phase (t >= 900 s); simulate() detects the
    "fcr" phase label and converts Δf → current with deadband + droop +
    SOC recovery. During commissioning (t < 900 s) the slot carries real
    current, exactly like the other two scenarios.
    """
    t = np.arange(T_STEPS) * DT
    sig = np.zeros(T_STEPS)                   # commission: current; FCR: Δf [Hz]
    ph = np.empty(T_STEPS, dtype='<U15')
    ica = np.zeros(T_STEPS, dtype=bool)
    C1 = C_NOM

    for k in range(T_STEPS):
        s = t[k]
        if s < 900:
            ph[k] = "commission"
            if   s < 60:  sig[k] = 0
            elif s < 120: sig[k] = +0.5*C1
            elif s < 180: sig[k] = 0
            elif s < 420: sig[k] = -0.4*C1
            elif s < 480: sig[k] = 0
            else:         sig[k] = +C1/6; ica[k] = True
        else:
            ph[k] = "fcr"

    # Synthetic grid-frequency deviation for t >= 900 s.
    # Nordic/Continental grids sit inside the ±10 mHz deadband ~80% of the time,
    # with occasional larger excursions. Model = Ornstein-Uhlenbeck wander
    # (mean-reverting) plus a few deterministic disturbance events.
    frng = np.random.default_rng(12345)      # fixed → identical frequency trace every run
    mask = t >= 900
    n = int(mask.sum())
    df = np.zeros(n)
    theta, sigma = 0.05, 0.004               # reversion rate [1/s], noise scale [Hz]
    noise = frng.standard_normal(n)
    for i in range(1, n):
        df[i] = df[i-1] - theta*df[i-1]*DT + sigma*np.sqrt(DT)*noise[i]
    gtime = 900 + np.arange(n)*DT
    for center, amp, width in [(1500, 0.09, 100), (3200, -0.12, 150), (5000, 0.07, 80)]:
        df += amp * np.exp(-((gtime - (900+center))/width)**2)
    sig[mask] = df
    return t, sig, ph, ica

# =====================================================================
#  5.  CORE SIMULATION
# =====================================================================
def simulate(rng, profile_fn, use_pybamm=True, verbose=False, soc_scenario=None):
    p = sample_pack(rng, soc_scenario=soc_scenario)
    t, I_cmd, phase, ica_win = profile_fn()

    # ── FCR: convert grid-frequency signal → current (closed-loop) ──
    # During the "fcr" phase, I_cmd currently holds Δf [Hz]. Convert to current
    # with a deadband + droop law and a SOC-recovery controller. Recovery acts
    # on a representative pack-mean SOC trajectory (physical EMS layer, not the
    # estimator), computed here so PyBaMM and the truth loop share one profile.
    is_fcr = (phase == "fcr")
    if is_fcr.any():
        C1 = C_NOM
        P_RATED   = 1.0 * C1          # rated FCR power = 1C
        DEADBAND  = 0.010             # ±10 mHz: no response
        DF_FULL   = 0.200             # ±200 mHz: full activation (Continental FCR)
        SOC_SET   = 0.50
        SOC_BAND  = 0.08              # start recovery beyond ±8% from setpoint
        P_RECOV   = 0.25 * C1         # recovery charge/discharge rate
        C_repr    = C_NOM * 0.90      # representative capacity for the EMS trajectory
        z_repr    = 0.50              # EMS assumes it starts mid-pack
        for k in range(T_STEPS):
            if not is_fcr[k]:
                continue
            df = I_cmd[k]             # Δf in Hz
            # Droop response: zero inside deadband, linear to full at DF_FULL.
            # Grid LOW (df<0) → inject power → BESS DISCHARGES (I<0, BMS convention).
            mag = max(0.0, (abs(df) - DEADBAND) / (DF_FULL - DEADBAND))
            mag = min(mag, 1.0)
            # Grid LOW (df<0): under-generation → BESS DISCHARGES (I<0) to support grid.
            # Grid HIGH (df>0): over-generation → BESS CHARGES (I>0) to absorb.
            I_fcr = np.sign(df) * mag * P_RATED
            # SOC recovery: if representative SOC drifts out of band, bias current
            # back toward setpoint (allowed via FCR deadband degrees of freedom).
            if z_repr > SOC_SET + SOC_BAND:
                I_fcr += -P_RECOV     # too full → lean discharge
            elif z_repr < SOC_SET - SOC_BAND:
                I_fcr += +P_RECOV     # too empty → lean charge
            # protect representative trajectory
            if I_fcr > 0 and z_repr > 0.79: I_fcr = 0.0
            if I_fcr < 0 and z_repr < 0.11: I_fcr = 0.0
            z_repr = np.clip(z_repr + I_fcr*DT/3600.0/C_repr, 0.10, 0.80)
            I_cmd[k] = I_fcr

    # PyBaMM cells
    pb_cells = build_pybamm_cells(I_cmd, rng) if (use_pybamm and HAS_PYBAMM) else []
    for ci in range(len(pb_cells)):
        if pb_cells[ci] is not None:
            p["z0"][ci] = pb_cells[ci]["init_soc"]
            p["soh"][ci] = 1.0; p["C"][ci] = C_NOM

    # Truth state
    z_true = p["z0"].copy()
    Up = np.zeros(N_CELLS)
    a_diff = np.exp(-DT / p["tau"])

    # ── BASELINE EKF: 2-state [z, R0] (voltage + coulomb counting) ──
    # R0 is estimated online here too, deliberately -- if only the enhanced
    # filter got to separate "wrong resistance" from "wrong SOC", a win for
    # it would be confounded with that structural advantage rather than
    # isolating what force+ICA fusion actually contributes. Giving both
    # filters the same [z, R0] voltage-only core makes the comparison fair;
    # the only remaining difference is the force/ICA channels below.
    z_cl  = np.full(N_CELLS, 0.45)
    R0_cl = np.full(N_CELLS, 0.9e-3)
    P_cl  = np.repeat(
        np.diag([0.30**2, (0.6e-3)**2])[None],
        N_CELLS, axis=0)
    Q_cl  = np.diag([(3e-4)**2, (1e-7)**2])

    # ── ENHANCED EKF: 3-state [z, R0, f_bias] ──
    # No capacity/SOH state: coulomb-counts against nameplate C_NOM, same as the
    # baseline filter. SOH is a separate BMS problem (capacity test, aging-trend
    # analysis, etc.), not something this SOC-focused filter estimates.
    z_en  = np.full(N_CELLS, 0.45)
    R0_en = np.full(N_CELLS, 0.9e-3)
    fb_en = np.full(N_CELLS, 200.0)
    P_en  = np.repeat(
        np.diag([0.30**2, (0.6e-3)**2, 20.0**2])[None],
        N_CELLS, axis=0)
    Q_en  = np.diag([(3e-4)**2, (1e-7)**2, (2e-3)**2])

    # Jia tanh arbitration
    MU_ARB = 5e4
    R_U_MAX, R_U_MIN = (SIG_V*6)**2, SIG_V**2
    Q_F_MAX, Q_F_MIN = (8e-3)**2, (1e-4)**2
    e_U = np.zeros(N_CELLS)

    # ICA buffers
    ica_buf  = np.zeros((N_CELLS, 50))
    ica_prev = np.zeros(N_CELLS)
    Vc_prev  = np.full(N_CELLS, np.nan)
    ica_done = np.zeros(N_CELLS, dtype=bool)
    ica_fires = 0

    # History
    Ztr = np.zeros((T_STEPS, N_CELLS))
    Zcl = np.zeros_like(Ztr); Zen = np.zeros_like(Ztr)
    Im_hist = np.zeros(T_STEPS)
    Fm_hist = np.zeros((T_STEPS, N_CELLS))

    for k in range(T_STEPS):
        I = I_cmd[k]
        if I > 0 and z_true.max() > 0.80 - 0.003: I = min(I, 0.0)
        if I < 0 and z_true.min() < 0.10 + 0.003: I = max(I, 0.0)

        # ── Truth ──
        z_true = np.clip(z_true + I*DT/3600.0/p["C"], 0.10, 0.80)
        Up = a_diff * Up + p["Rp"]*(1-a_diff)*I

        V_true = np.zeros(N_CELLS)
        for i in range(N_CELLS):
            if i < len(pb_cells) and pb_cells[i] is not None:
                V_true[i] = pb_cells[i]["V"][k]
                z_true[i] = pb_cells[i]["soc"][k]
            else:
                V_true[i] = p["ocv_fns"][i](z_true[i]) + I*p["R0"][i] + Up[i]
        F_true = np.array([force_true(z_true[i], i, p) for i in range(N_CELLS)])

        # ── Sensors ──
        Vm = V_true + rng.normal(0, SIG_V, N_CELLS)
        Fm = F_true + rng.normal(0, SIG_F, N_CELLS)
        Im = I * p["i_gain"] + p["i_off"] + rng.normal(0, SIG_I)

        Ztr[k] = z_true; Im_hist[k] = Im; Fm_hist[k] = Fm

        # ============================================================
        #  BASELINE EKF  2-state: [z, R0]  (voltage + coulomb counting)
        # ============================================================
        z_cl = np.clip(z_cl + Im*DT/3600.0/C_NOM, 0.0, 1.0)
        P_cl = P_cl + Q_cl

        V_pred_cl = ocv_nom(z_cl) + Im*R0_cl
        yV_cl = Vm - V_pred_cl
        Hc = np.zeros((N_CELLS, 2))
        Hc[:, 0] = docv_dz_nom(z_cl); Hc[:, 1] = Im
        Rc = np.full(N_CELLS, SIG_V**2)
        Sc = np.einsum('ni,nij,nj->n', Hc, P_cl, Hc) + Rc
        Kc = np.einsum('nij,nj->ni', P_cl, Hc) / Sc[:, None]
        z_cl  = np.clip(z_cl  + Kc[:, 0]*yV_cl, 0.0, 1.0)
        R0_cl = np.clip(R0_cl + Kc[:, 1]*yV_cl, 0.05e-3, 4e-3)
        P_cl -= np.einsum('ni,nj,njk->nik', Kc, Hc, P_cl)
        P_cl = 0.5*(P_cl + P_cl.transpose(0, 2, 1))
        Zcl[k] = z_cl

        # ============================================================
        #  ENHANCED EKF  3-state: [z, R0, f_bias]
        # ============================================================
        # ── Predict ── coulomb-counts against nameplate C_NOM, exactly like
        # the baseline filter -- no capacity state, so the time-update is a
        # plain additive step (state-transition Jacobian is identity).
        z_en = np.clip(z_en + Im * DT / 3600.0 / C_NOM, 0.0, 1.0)
        P_en = P_en + Q_en

        # ── Jia tanh arbitration ──
        R_U_k = R_U_MAX - (R_U_MAX - R_U_MIN)*np.tanh(MU_ARB * e_U**2)
        Q_F_k = Q_F_MAX - (Q_F_MAX - Q_F_MIN)*np.tanh(MU_ARB * e_U**2)

        # ── Update 1: Voltage ──
        V_pred = ocv_nom(z_en) + Im*R0_en
        yV_en = Vm - V_pred
        e_U = 0.95*e_U + 0.05*yV_en

        Hv = np.zeros((N_CELLS, 3))
        Hv[:, 0] = docv_dz_nom(z_en); Hv[:, 1] = Im
        Rv = R_U_k
        Sv = np.einsum('ni,nij,nj->n', Hv, P_en, Hv) + Rv
        Kv = np.einsum('nij,nj->ni', P_en, Hv) / Sv[:, None]
        z_en  = np.clip(z_en  + Kv[:, 0]*yV_en, 0.0, 1.0)
        R0_en = np.clip(R0_en + Kv[:, 1]*yV_en, 0.05e-3, 4e-3)
        P_en -= np.einsum('ni,nj,njk->nik', Kv, Hv, P_en)
        P_en = 0.5*(P_en + P_en.transpose(0, 2, 1))

        # ── Update 2: Force ──
        yF = Fm - (fb_en + force_nom(z_en))
        Hf = np.zeros((N_CELLS, 3))
        Hf[:, 0] = dforce_dz_nom(z_en); Hf[:, 2] = 1.0

        infl = np.clip(np.abs(Hf[:, 0]) / 18.0, 0.04, 1.0)
        Rf = (SIG_F**2) / infl
        P_en[:, 0, 0] += Q_F_k                     # adaptive process noise

        Sf = np.einsum('ni,nij,nj->n', Hf, P_en, Hf) + Rf
        Kf = np.einsum('nij,nj->ni', P_en, Hf) / Sf[:, None]
        z_en  = np.clip(z_en  + Kf[:, 0]*yF, 0.0, 1.0)
        fb_en += Kf[:, 2]*yF
        P_en -= np.einsum('ni,nj,njk->nik', Kf, Hf, P_en)
        P_en = 0.5*(P_en + P_en.transpose(0, 2, 1))

        # ── Update 3: ICA (C/6 window only) ──
        if ica_win[k]:
            Vc = Vm - Im*R0_en
            if np.isnan(Vc_prev[0]):
                Vc_prev = Vc.copy()
            else:
                dV = Vc - Vc_prev; Vc_prev = Vc.copy()
                ica_val = np.abs(Im*DT/3600.0) / (np.abs(dV) + 1e-7)
                ica_buf = np.roll(ica_buf, -1, axis=1)
                ica_buf[:, -1] = ica_val
                ica_sm = ica_buf.mean(axis=1)
                fired = ((ica_sm > 8.0) & (k > 100) &
                         (ica_sm < ica_prev) & (~ica_done) &
                         (np.abs(docv_dz_nom(z_en)) > 0.02))
                if fired.any():
                    ica_fires += int(fired.sum())
                    z_en[fired] = ICA_PEAK_SOC
                    P_en[fired, 0, 0] = 0.02**2
                    ica_done[fired] = True
                ica_prev = ica_sm.copy()

        Zen[k] = z_en

    # ── Metrics ──
    err_cl = Zcl - Ztr; err_en = Zen - Ztr
    k15 = min(int(900/DT), T_STEPS-1)

    def conv_min(Zest, thr=0.025):
        e = np.abs(Zest.mean(1) - Ztr.mean(1))
        for kk in range(200, T_STEPS):
            if e[kk] < thr and np.all(e[kk:min(kk+300, T_STEPS)] < thr*1.5):
                return kk*DT/60
        return np.nan

    metrics = dict(
        rmse_pack_cl = float(np.sqrt(np.mean((Zcl[k15:].mean(1)-Ztr[k15:].mean(1))**2))),
        rmse_pack_en = float(np.sqrt(np.mean((Zen[k15:].mean(1)-Ztr[k15:].mean(1))**2))),
        mae_cell_cl15 = float(np.mean(np.abs(err_cl[k15]))),
        mae_cell_en15 = float(np.mean(np.abs(err_en[k15]))),
        mae_cell_end_cl = float(np.mean(np.abs(err_cl[-1]))),
        mae_cell_end_en = float(np.mean(np.abs(err_en[-1]))),
        conv_cl = conv_min(Zcl), conv_en = conv_min(Zen),
        ica_fires = ica_fires)

    full = dict(t=t, I_cmd=I_cmd, phase=phase, ica_win=ica_win, Im=Im_hist,
                Ztr=Ztr, Zcl=Zcl, Zen=Zen, err_cl=err_cl, err_en=err_en,
                Fm=Fm_hist, p=p,
                metrics=metrics)
    return full, metrics

# =====================================================================
#  6.  PLOTTING (per scenario)
# =====================================================================
def make_plots(full, tag, out, error_only=False):
    """error_only=True: skip everything except the per-cell error+RMSE plot
    (used for the pseudo-random SOC-scenario sweep, which only needs that)."""
    os.makedirs(out, exist_ok=True)
    plt.rcParams.update({"font.size": 10, "axes.grid": True, "grid.alpha": .25})
    tm = full["t"]/60; m = full["metrics"]

    if error_only:
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
        for i in range(N_CELLS):
            a1.plot(tm, full["err_cl"][:, i]*100, "r-", alpha=.12, lw=.5)
            a2.plot(tm, full["err_en"][:, i]*100, "b-", alpha=.12, lw=.5)
        rmse_cl = np.sqrt((full["err_cl"]**2).mean(1))*100
        rmse_en = np.sqrt((full["err_en"]**2).mean(1))*100
        a1.plot(tm, rmse_cl, "k-", lw=2.5, label="RMSE across cells")
        a2.plot(tm, rmse_en, "k-", lw=2.5, label="RMSE across cells")
        for a in (a1, a2):
            a.axhline(0, color="k", lw=.5, ls="--")
            for h in [2, -2]: a.axhline(h, color="g", lw=.6, ls=":")
            a.set_ylabel("SOC error [%]"); a.set_ylim(-40, 40); a.legend(fontsize=9)
        a1.set_title(f"{tag} — Baseline EKF (V+CC+R0) per-cell error  |  RMSE@end={rmse_cl[-1]:.1f}%")
        a2.set_title(f"{tag} — Enhanced EKF per-cell error  |  RMSE@end={rmse_en[-1]:.1f}%")
        a2.set_xlabel("time [min]")
        fig.tight_layout(); fig.savefig(f"{out}/{tag}_error.png", dpi=140); plt.close(fig)
        return

    # 1 — Pack SOC + current
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 7), sharex=True,
                                  gridspec_kw={"height_ratios": [2.5, 1]})
    a1.plot(tm, full["Ztr"].mean(1)*100, "k-",  lw=3,   label="True pack SOC")
    a1.plot(tm, full["Zcl"].mean(1)*100, "r--", lw=1.8, label="Baseline EKF (V+CC+R0)")
    a1.plot(tm, full["Zen"].mean(1)*100, "b-",  lw=1.8, label="Enhanced EKF (V+CC+R0+F+ICA)")
    a1.set(ylabel="SOC [%]", ylim=(0, 100),
           title=f"{tag} — Pack SOC  |  RMSE: baseline={m['rmse_pack_cl']*100:.1f}% "
                 f"enhanced={m['rmse_pack_en']*100:.1f}%")
    a1.legend(loc="upper right", fontsize=9)
    a2.plot(tm, full["I_cmd"]/C_NOM, "g-", lw=0.8)
    a2.axhline(0, color="k", lw=.5); a2.set(ylabel="C-rate", xlabel="time [min]", ylim=(-1.1, 1.1))
    fig.tight_layout(); fig.savefig(f"{out}/{tag}_1_pack_soc.png", dpi=140); plt.close(fig)

    # 2 — Per-cell error
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
    for i in range(N_CELLS):
        a1.plot(tm, full["err_cl"][:, i]*100, "r-", alpha=.12, lw=.5)
        a2.plot(tm, full["err_en"][:, i]*100, "b-", alpha=.12, lw=.5)
    rmse_cl = np.sqrt((full["err_cl"]**2).mean(1))*100
    rmse_en = np.sqrt((full["err_en"]**2).mean(1))*100
    a1.plot(tm, rmse_cl, "k-", lw=2.5, label="RMSE across cells")
    a2.plot(tm, rmse_en, "k-", lw=2.5, label="RMSE across cells")
    for a in (a1, a2):
        a.axhline(0, color="k", lw=.5, ls="--")
        for h in [2, -2]: a.axhline(h, color="g", lw=.6, ls=":")
        a.set_ylabel("SOC error [%]"); a.set_ylim(-40, 40); a.legend(fontsize=9)
    a1.set_title(f"{tag} — Baseline EKF (V+CC+R0) per-cell error  |  RMSE@end={rmse_cl[-1]:.1f}%")
    a2.set_title(f"{tag} — Enhanced EKF per-cell error  |  RMSE@end={rmse_en[-1]:.1f}%")
    a2.set_xlabel("time [min]")
    fig.tight_layout(); fig.savefig(f"{out}/{tag}_2_error.png", dpi=140); plt.close(fig)

    # 3 — Scatter at 15 min and end
    k15 = min(int(900/DT), T_STEPS-1)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5.5))
    for ax, kk, ttl in ((a1, k15, "t = 15 min"), (a2, T_STEPS-1, f"t = {SIM_MIN} min")):
        ax.plot([5, 85], [5, 85], "k-", lw=.6, alpha=.4)
        ax.scatter(full["Ztr"][kk]*100, full["Zcl"][kk]*100, c="r", marker="x", s=50, label="Baseline")
        ax.scatter(full["Ztr"][kk]*100, full["Zen"][kk]*100, c="b", marker="o", s=28,
                   facecolors="none", lw=1.2, label="Enhanced")
        ax.set(title=f"Per-cell SOC at {ttl}", xlabel="true SOC [%]",
               ylabel="est SOC [%]", xlim=(5, 85), ylim=(5, 85), aspect="equal")
        ax.legend(fontsize=9)
    fig.suptitle(tag, fontsize=12, weight="bold")
    fig.tight_layout(); fig.savefig(f"{out}/{tag}_3_scatter.png", dpi=140); plt.close(fig)

    # 4 — Force signal for 3 cells
    fig, ax = plt.subplots(figsize=(13, 4))
    for i in range(min(3, N_CELLS)):
        ax.plot(tm, full["Fm"][:, i], lw=.5, alpha=.7, label=f"cell {i}")
    ax.set(ylabel="force [N]", xlabel="time [min]",
           title=f"{tag} — Per-cell force (non-monotonic → SOC info inside OCV plateau)")
    ax.legend(fontsize=8); fig.tight_layout()
    fig.savefig(f"{out}/{tag}_4_force.png", dpi=140); plt.close(fig)

    # 5 — Error vs true SOC
    fig, ax = plt.subplots(figsize=(12, 4.5))
    zz = np.linspace(0.10, 0.80, 500)
    dv = np.abs(docv_dz_nom(zz))
    flat_lo = zz[dv < 0.06].min()*100; flat_hi = zz[dv < 0.06].max()*100
    ax.axvspan(flat_lo, flat_hi, color="#ffe9e9", label=f"OCV plateau ({flat_lo:.0f}–{flat_hi:.0f}%)")
    ax.scatter(full["Ztr"][-1]*100, full["err_en"][-1]*100, c="b", s=28, label="Enhanced")
    ax.scatter(full["Ztr"][-1]*100, full["err_cl"][-1]*100, c="r", marker="x", s=40, label="Baseline")
    ax.axhline(0, color="k", lw=.6)
    ax.set(title=f"{tag} — Final error vs true SOC",
           xlabel="true SOC [%]", ylabel="SOC error [%]", xlim=(5, 85))
    ax.legend(fontsize=9); fig.tight_layout()
    fig.savefig(f"{out}/{tag}_5_error_vs_soc.png", dpi=140); plt.close(fig)


# =====================================================================
#  7.  PHYSICS REFERENCE PLOT
# =====================================================================
def plot_physics(out):
    os.makedirs(out, exist_ok=True)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5))
    zz = np.linspace(0, 1, 500)
    a1.plot(zz*100, ocv_nom(zz), "b-", lw=2)
    # Shade plateau
    dv = np.abs(docv_dz_nom(zz))
    flat = dv < 0.06
    if flat.any():
        a1.axvspan(zz[flat].min()*100, zz[flat].max()*100, color="#ffe9e9", alpha=.5,
                   label=f"plateau ({zz[flat].min()*100:.0f}–{zz[flat].max()*100:.0f}%)")
    a1.set(title="LFP OCV(SOC) — 280 Ah CATL\n~40 mV span across SOC 0.15–0.85",
           xlabel="SOC [%]", ylabel="OCV [V]")
    a1.legend(fontsize=8)

    a2.plot(zz*100, force_nom(zz), "m-", lw=2, label="force shape (no preload)")
    a2r = a2.twinx()
    a2r.plot(zz*100, np.abs(dforce_dz_nom(zz)), "g--", lw=1, alpha=.5, label="|dF/dz|")
    a2.set(title="Force(SOC) — non-monotonic\ninflections ~30% and ~70%",
           xlabel="SOC [%]", ylabel="force [N]")
    a2r.set_ylabel("|dF/dz|")
    h1, l1 = a2.get_legend_handles_labels()
    h2, l2 = a2r.get_legend_handles_labels()
    a2.legend(h1+h2, l1+l2, fontsize=8)
    fig.tight_layout(); fig.savefig(f"{out}/physics.png", dpi=140); plt.close(fig)


# =====================================================================
#  8.  ALGORITHM DIAGRAM
# =====================================================================
def make_diagram(out):
    os.makedirs(out, exist_ok=True)
    from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
    fig, ax = plt.subplots(figsize=(15, 9)); ax.axis("off")
    ax.set_xlim(0, 15); ax.set_ylim(0, 9)

    def box(x, y, w, h, txt, fc, fs=8.5):
        ax.add_patch(FancyBboxPatch((x, y), w, h,
            boxstyle="round,pad=.04,rounding_size=.15", fc=fc, ec="#333", lw=1.2))
        ax.text(x+w/2, y+h/2, txt, ha="center", va="center", fontsize=fs,
                family="monospace", linespacing=1.3)
    def arr(x1, y1, x2, y2, c="#444"):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>",
            mutation_scale=13, lw=1.2, color=c))

    box(0.2, 3.8, 1.8, 1.4, "20 LFP cells\nunknown SOC\n(SOH out of scope)", "#eee")
    box(2.4, 6.4, 2.1, 0.8, "Voltage V\n(3 mV)", "#dce8fb")
    box(2.4, 4.4, 2.1, 0.8, "Current I\n(bias+noise)", "#dce8fb")
    box(2.4, 2.4, 2.1, 0.8, "Force F/cell\n(3 N)", "#dcefe0")
    for yy in (6.8, 4.8, 2.8): arr(2.0, 4.5, 2.4, yy)

    box(5.2, 5.9, 3.4, 1.7,
        "BASELINE EKF: 2-state [z, R0]\n"
        " predict: z += I·Δt/C_nom\n"
        " update: y = V-OCV(z)-I·R0\n"
        " H = [dOCV/dz, I]\n"
        " R0 estimated too (fair fight --\n"
        " only force/ICA differ vs Enhanced)\n"
        " but dOCV/dz → ≈0 in plateau,\n"
        " so z is still BLIND there", "#fbe3e6")
    arr(4.5, 6.8, 5.2, 7.0); arr(4.5, 4.8, 5.1, 6.2)
    box(9.2, 6.3, 2.9, 0.9, "SOC drifts with CC bias", "#fbe3e6")
    arr(8.6, 6.8, 9.2, 6.7)

    box(5.2, 0.3, 3.6, 5.2,
        "ENHANCED 3-STATE EKF\n"
        " state: x = [z, R₀, f_bias]\n"
        " ─── predict ───\n"
        " z += I·Δt/C_nom  (same as baseline)\n"
        " ─── V update (IR corrected) ───\n"
        " y = V - OCV(z) - I·R₀\n"
        " H = [dOCV/dz, I, 0]\n"
        " R_U adaptive (Jia Eq.14)\n"
        " ─── Force update ───\n"
        " y = F - f_bias - F_nom(z)\n"
        " H = [dF/dz, 0, 1]\n"
        " inflection gate |dF/dz|\n"
        " ─── ICA (C/6 only, Fly §4.1)\n"
        " dQ/dV peak → z := z*", "#e0edff", fs=7.8)
    arr(4.5, 6.8, 5.1, 5.3, c="#25a"); arr(4.5, 4.8, 5.2, 4.2); arr(4.5, 2.8, 5.2, 2.3, c="#187")

    box(9.3, 3.4, 3.0, 1.8,
        "TANH ARBITRATION\n(Jia Eq.13-14)\n"
        " e_U = V_meas - V_pred\n"
        " R_U = R_max-ΔR·tanh(μ·e²)\n"
        " Q_F = Q_max-ΔQ·tanh(μ·e²)\n"
        "large e_U → trust force\n"
        "small e_U → trust voltage", "#fff2d9", fs=8)
    arr(8.8, 3.8, 9.3, 4.0); arr(9.3, 3.6, 8.8, 3.0, c="#c80")

    box(9.3, 0.6, 3.0, 2.2,
        "OUTPUTS\n"
        " • pack SOC (converges ~15 min)\n"
        " • per-cell SOC (2-5% MAE)\n"
        " • per-cell R₀\n"
        " (SOH: separate BMS method,\n"
        "  not estimated here)", "#e3f7e8")
    arr(8.8, 1.8, 9.3, 1.6)

    ax.text(7.5, 8.6,
        "3-State Force+ICA EKF  vs  2-State Baseline V+CC+R0 EKF  (LFP 280 Ah)\n"
        "Fly & Chen (2020) J. Energy Storage  ·  Jia et al. (2024) IEEE ITEC",
        ha="center", fontsize=12, weight="bold", linespacing=1.5)
    fig.tight_layout(); fig.savefig(f"{out}/algorithm.png", dpi=140); plt.close(fig)


# =====================================================================
#  9.  MONTE CARLO (per scenario, empirical only for speed)
# =====================================================================
def monte_carlo(profile_fn, tag, n=30, seed=0, soc_scenario=None):
    rng = np.random.default_rng(seed)
    keys = ["rmse_pack_cl","rmse_pack_en","mae_cell_end_cl","mae_cell_end_en",
            "conv_cl","conv_en"]
    acc = {k: [] for k in keys}
    for j in range(n):
        _, met = simulate(np.random.default_rng(rng.integers(1<<31)),
                          profile_fn, use_pybamm=False, soc_scenario=soc_scenario)
        for k in keys: acc[k].append(met[k])
        if (j+1) % 10 == 0: print(f"  {tag} MC {j+1}/{n}")
    return {k: np.array(v, float) for k, v in acc.items()}

def plot_mc(mc_ps, mc_pv, mc_fcr, out):
    os.makedirs(out, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))
    for ax, key, title in [
        (axes[0], "rmse_pack", "Pack RMSE (post-commission)"),
        (axes[1], "mae_cell_end", "Per-cell MAE at end")]:
        data = [mc_ps[f"{key}_cl"]*100, mc_ps[f"{key}_en"]*100,
                mc_pv[f"{key}_cl"]*100, mc_pv[f"{key}_en"]*100,
                mc_fcr[f"{key}_cl"]*100, mc_fcr[f"{key}_en"]*100]
        bp = ax.boxplot(data,
                        tick_labels=["PS-Cls","PS-Enh","PV-Cls","PV-Enh","FCR-Cls","FCR-Enh"],
                        patch_artist=True)
        cols = ["#f4a0a0","#a0c4f4"]*3
        for patch, c in zip(bp["boxes"], cols): patch.set_facecolor(c)
        ax.set(title=title, ylabel="[%]")
        ax.tick_params(axis='x', labelsize=8, rotation=20)
    fig.suptitle("Monte Carlo comparison across 3 scenarios (n=20 packs each)",
                 fontsize=12, weight="bold")
    fig.tight_layout(); fig.savefig(f"{out}/montecarlo.png", dpi=140); plt.close(fig)


SCENARIOS = dict(PeakShaving=profile_peak_shaving, PV=profile_pv, FCR=profile_fcr)
SOC_SCENARIOS = {
    "A_cluster20_35":       "A",   # 13 cells 20-35%, rest random
    "B_cluster10_15_25_35": "B",   # 5 cells 10-15%, 5 cells 25-35%, rest random
}

def run_main3(out="bms_plots"):
    """3 full duty-cycle scenarios (PS/PV/FCR), single random pack each,
    complete plot suite + printed metrics. No SOH/capacity state."""
    os.makedirs(out, exist_ok=True)
    results = {}
    for tag, prof in SCENARIOS.items():
        print(f"\n═══ {tag} (120 min) ═══")
        full, met = simulate(np.random.default_rng(), prof, use_pybamm=True, verbose=True)
        make_plots(full, tag, out)
        for k, v in met.items(): print(f"  {k:22s}: {v}")
        results[tag] = met
    return results

def run_soc_scenarios(out="bms_plots_soc_scenarios"):
    """2 pseudo-random initial-SOC scenarios x 3 duty cycles = 6 runs.
    Empirical cells only (use_pybamm=False, for speed and so the SOC
    clustering assignment applies cleanly to all 20 cells). Error+RMSE
    plot only, per the request."""
    os.makedirs(out, exist_ok=True)
    for soc_name, soc_code in SOC_SCENARIOS.items():
        for tag, prof in SCENARIOS.items():
            run_tag = f"{tag}_{soc_name}"
            print(f"\n═══ {run_tag} ═══")
            full, met = simulate(np.random.default_rng(), prof, use_pybamm=False,
                                  soc_scenario=soc_code)
            make_plots(full, run_tag, out, error_only=True)
            print(f"  rmse_pack_cl: {met['rmse_pack_cl']*100:.2f}%   "
                  f"rmse_pack_en: {met['rmse_pack_en']*100:.2f}%")

def run_diagrams(out="bms_plots"):
    os.makedirs(out, exist_ok=True)
    zz = np.linspace(0.15, 0.85, 500)
    dv = np.abs(docv_dz_nom(zz))
    flat_pct = 100 * np.sum(dv < 0.06) / len(dv)
    v_span = (ocv_nom(0.85) - ocv_nom(0.15)) * 1000
    print(f"OCV plateau: {v_span:.0f} mV span over SOC 0.15-0.85, "
          f"{flat_pct:.0f}% of band has |dOCV/dz| < 0.06")
    print(f"ICA peak SOC = {ICA_PEAK_SOC:.3f}")
    make_diagram(out)
    plot_physics(out)

def run_mc(out="bms_plots"):
    print("\n═══ MONTE CARLO (20 packs × 3 scenarios) ═══")
    mc_ps  = monte_carlo(profile_peak_shaving, "PS", n=20)
    mc_pv  = monte_carlo(profile_pv, "PV", n=20)
    mc_fcr = monte_carlo(profile_fcr, "FCR", n=20)
    plot_mc(mc_ps, mc_pv, mc_fcr, out)
    def q(a): return f"{np.nanmedian(a):.2f}  [{np.nanpercentile(a,10):.2f}, {np.nanpercentile(a,90):.2f}]"
    for tag, mc in [("PeakShaving", mc_ps), ("PV", mc_pv), ("FCR", mc_fcr)]:
        print(f"\n  {tag}:")
        print(f"    pack RMSE baseline  : {q(mc['rmse_pack_cl']*100)} %")
        print(f"    pack RMSE enhanced  : {q(mc['rmse_pack_en']*100)} %")

# =====================================================================
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("all", "diagrams"): run_diagrams()
    if cmd in ("all", "main3"):    run_main3()
    if cmd in ("all", "soc"):      run_soc_scenarios()
    if cmd in ("all", "mc"):       run_mc()
