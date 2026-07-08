#!/usr/bin/env python3
"""
FINAL ABLATION TEST (not part of the main deliverable) --
Does the enhanced filter's coulomb-counting (CC) predict step actually help,
or is it fighting the persistent current-sensor bias for no net benefit given
it already has voltage + force + ICA doing most of the work?

Three filters, run side by side on the same truth/sensors each step:
  1. Baseline      -- 2-state [z, R0], CC predict + voltage update (existing)
  2. Enhanced       -- 3-state [z, R0, f_bias], CC predict + V+F+ICA (existing)
  3. Enhanced-noCC  -- identical to (2) except the predict step does NOT
                       integrate current at all: z_pred = z_old (a pure
                       random-walk state), only process noise lets it move.
                       All three measurement updates (V, F, ICA) are
                       unchanged. This isolates whether CC integration is
                       net helpful once force+ICA are already fusing in.

Empirical cells only (no PyBaMM) for speed; Monte Carlo over n packs/scenario.
"""
import numpy as np
import bess_online_ekf as m

N_CELLS, C_NOM, DT, T_STEPS = m.N_CELLS, m.C_NOM, m.DT, m.T_STEPS
SIG_V, SIG_F, SIG_I = m.SIG_V, m.SIG_F, m.SIG_I
ocv_nom, docv_dz_nom = m.ocv_nom, m.docv_dz_nom
force_nom, dforce_dz_nom = m.force_nom, m.dforce_dz_nom
ICA_PEAK_SOC = m.ICA_PEAK_SOC


def simulate_3way(rng, profile_fn, soc_scenario=None):
    p = m.sample_pack(rng, soc_scenario=soc_scenario)
    t, I_cmd, phase, ica_win = profile_fn()

    is_fcr = (phase == "fcr")
    if is_fcr.any():
        C1 = C_NOM
        P_RATED, DEADBAND, DF_FULL = 1.0*C1, 0.010, 0.200
        SOC_SET, SOC_BAND, P_RECOV = 0.50, 0.08, 0.25*C1
        C_repr, z_repr = C_NOM*0.90, 0.50
        for k in range(T_STEPS):
            if not is_fcr[k]:
                continue
            df = I_cmd[k]
            mag = min(1.0, max(0.0, (abs(df)-DEADBAND)/(DF_FULL-DEADBAND)))
            I_fcr = np.sign(df) * mag * P_RATED
            if z_repr > SOC_SET+SOC_BAND: I_fcr += -P_RECOV
            elif z_repr < SOC_SET-SOC_BAND: I_fcr += +P_RECOV
            if I_fcr > 0 and z_repr > 0.79: I_fcr = 0.0
            if I_fcr < 0 and z_repr < 0.11: I_fcr = 0.0
            z_repr = np.clip(z_repr + I_fcr*DT/3600.0/C_repr, 0.10, 0.80)
            I_cmd[k] = I_fcr

    z_true = p["z0"].copy(); Up = np.zeros(N_CELLS)
    a_diff = np.exp(-DT/p["tau"])

    # Baseline: 2-state [z, R0]
    z_cl = np.full(N_CELLS, 0.45); R0_cl = np.full(N_CELLS, 0.9e-3)
    P_cl = np.repeat(np.diag([0.30**2, (0.6e-3)**2])[None], N_CELLS, 0)
    Q_cl = np.diag([(3e-4)**2, (1e-7)**2])

    # Enhanced (CC): 3-state [z, R0, f_bias]
    z_en = np.full(N_CELLS, 0.45); R0_en = np.full(N_CELLS, 0.9e-3); fb_en = np.full(N_CELLS, 200.0)
    P_en = np.repeat(np.diag([0.30**2, (0.6e-3)**2, 20.0**2])[None], N_CELLS, 0)
    Q_en = np.diag([(3e-4)**2, (1e-7)**2, (2e-3)**2])

    # Enhanced-noCC: same structure, but z prediction has NO current integration.
    # Give it a somewhat larger z process-noise than Q_en's, since without CC
    # the only thing letting z move between measurement updates is Q -- too
    # small and it can't track real SOC changes at all between updates.
    z_nc = np.full(N_CELLS, 0.45); R0_nc = np.full(N_CELLS, 0.9e-3); fb_nc = np.full(N_CELLS, 200.0)
    P_nc = np.repeat(np.diag([0.30**2, (0.6e-3)**2, 20.0**2])[None], N_CELLS, 0)
    Q_nc = np.diag([(4e-3)**2, (1e-7)**2, (2e-3)**2])   # larger z process noise than Q_en

    MU_ARB = 5e4
    R_U_MAX, R_U_MIN = (SIG_V*6)**2, SIG_V**2
    Q_F_MAX, Q_F_MIN = (8e-3)**2, (1e-4)**2
    e_U_en = np.zeros(N_CELLS); e_U_nc = np.zeros(N_CELLS)

    ica_buf_en = np.zeros((N_CELLS, 50)); ica_prev_en = np.zeros(N_CELLS)
    Vc_prev_en = np.full(N_CELLS, np.nan); ica_done_en = np.zeros(N_CELLS, dtype=bool)
    ica_buf_nc = np.zeros((N_CELLS, 50)); ica_prev_nc = np.zeros(N_CELLS)
    Vc_prev_nc = np.full(N_CELLS, np.nan); ica_done_nc = np.zeros(N_CELLS, dtype=bool)
    ica_fires_en = 0; ica_fires_nc = 0

    Ztr = np.zeros((T_STEPS, N_CELLS))
    Zcl = np.zeros_like(Ztr); Zen = np.zeros_like(Ztr); Znc = np.zeros_like(Ztr)

    for k in range(T_STEPS):
        I = I_cmd[k]
        if I > 0 and z_true.max() > 0.80-0.003: I = min(I, 0.0)
        if I < 0 and z_true.min() < 0.10+0.003: I = max(I, 0.0)

        z_true = np.clip(z_true + I*DT/3600.0/p["C"], 0.10, 0.80)
        Up = a_diff*Up + p["Rp"]*(1-a_diff)*I
        V_true = np.array([p["ocv_fns"][i](z_true[i]) + I*p["R0"][i] + Up[i] for i in range(N_CELLS)])
        F_true = np.array([m.force_true(z_true[i], i, p) for i in range(N_CELLS)])

        Vm = V_true + rng.normal(0, SIG_V, N_CELLS)
        Fm = F_true + rng.normal(0, SIG_F, N_CELLS)
        Im = I*p["i_gain"] + p["i_off"] + rng.normal(0, SIG_I)
        Ztr[k] = z_true

        # ---- Baseline 2-state ----
        z_cl = np.clip(z_cl + Im*DT/3600.0/C_NOM, 0.0, 1.0); P_cl = P_cl + Q_cl
        yV = Vm - (ocv_nom(z_cl) + Im*R0_cl)
        Hc = np.zeros((N_CELLS, 2)); Hc[:, 0] = docv_dz_nom(z_cl); Hc[:, 1] = Im
        Sc = np.einsum('ni,nij,nj->n', Hc, P_cl, Hc) + SIG_V**2
        Kc = np.einsum('nij,nj->ni', P_cl, Hc)/Sc[:, None]
        z_cl = np.clip(z_cl + Kc[:, 0]*yV, 0.0, 1.0); R0_cl = np.clip(R0_cl + Kc[:, 1]*yV, 0.05e-3, 4e-3)
        P_cl -= np.einsum('ni,nj,njk->nik', Kc, Hc, P_cl); P_cl = 0.5*(P_cl+P_cl.transpose(0, 2, 1))
        Zcl[k] = z_cl

        # ---- Enhanced (CC) 3-state ----
        z_en = np.clip(z_en + Im*DT/3600.0/C_NOM, 0.0, 1.0); P_en = P_en + Q_en
        R_U_k = R_U_MAX - (R_U_MAX-R_U_MIN)*np.tanh(MU_ARB*e_U_en**2)
        Q_F_k = Q_F_MAX - (Q_F_MAX-Q_F_MIN)*np.tanh(MU_ARB*e_U_en**2)
        yV = Vm - (ocv_nom(z_en) + Im*R0_en); e_U_en = 0.95*e_U_en + 0.05*yV
        Hv = np.zeros((N_CELLS, 3)); Hv[:, 0] = docv_dz_nom(z_en); Hv[:, 1] = Im
        Sv = np.einsum('ni,nij,nj->n', Hv, P_en, Hv) + R_U_k
        Kv = np.einsum('nij,nj->ni', P_en, Hv)/Sv[:, None]
        z_en = np.clip(z_en + Kv[:, 0]*yV, 0.0, 1.0); R0_en = np.clip(R0_en + Kv[:, 1]*yV, 0.05e-3, 4e-3)
        P_en -= np.einsum('ni,nj,njk->nik', Kv, Hv, P_en); P_en = 0.5*(P_en+P_en.transpose(0, 2, 1))
        yF = Fm - (fb_en + force_nom(z_en))
        Hf = np.zeros((N_CELLS, 3)); Hf[:, 0] = dforce_dz_nom(z_en); Hf[:, 2] = 1.0
        infl = np.clip(np.abs(Hf[:, 0])/18.0, 0.04, 1.0); Rf = (SIG_F**2)/infl
        P_en[:, 0, 0] += Q_F_k
        Sf = np.einsum('ni,nij,nj->n', Hf, P_en, Hf) + Rf
        Kf = np.einsum('nij,nj->ni', P_en, Hf)/Sf[:, None]
        z_en = np.clip(z_en + Kf[:, 0]*yF, 0.0, 1.0); fb_en += Kf[:, 2]*yF
        P_en -= np.einsum('ni,nj,njk->nik', Kf, Hf, P_en); P_en = 0.5*(P_en+P_en.transpose(0, 2, 1))
        if ica_win[k]:
            Vc = Vm - Im*R0_en
            if np.isnan(Vc_prev_en[0]): Vc_prev_en = Vc.copy()
            else:
                dV = Vc - Vc_prev_en; Vc_prev_en = Vc.copy()
                ica_val = np.abs(Im*DT/3600.0)/(np.abs(dV)+1e-7)
                ica_buf_en = np.roll(ica_buf_en, -1, axis=1); ica_buf_en[:, -1] = ica_val
                ica_sm = ica_buf_en.mean(axis=1)
                fired = ((ica_sm > 8.0) & (k > 100) & (ica_sm < ica_prev_en) &
                          (~ica_done_en) & (np.abs(docv_dz_nom(z_en)) > 0.02))
                if fired.any():
                    ica_fires_en += int(fired.sum())
                    z_en[fired] = ICA_PEAK_SOC; P_en[fired, 0, 0] = 0.02**2; ica_done_en[fired] = True
                ica_prev_en = ica_sm.copy()
        Zen[k] = z_en

        # ---- Enhanced-noCC 3-state (NO current integration in predict) ----
        # z_nc prediction: no += Im*DT/... term. State just carries forward,
        # process noise Q_nc[0,0] is what allows the estimate to move at all
        # between measurement corrections.
        P_nc = P_nc + Q_nc
        R_U_k2 = R_U_MAX - (R_U_MAX-R_U_MIN)*np.tanh(MU_ARB*e_U_nc**2)
        Q_F_k2 = Q_F_MAX - (Q_F_MAX-Q_F_MIN)*np.tanh(MU_ARB*e_U_nc**2)
        yV2 = Vm - (ocv_nom(z_nc) + Im*R0_nc); e_U_nc = 0.95*e_U_nc + 0.05*yV2
        Hv2 = np.zeros((N_CELLS, 3)); Hv2[:, 0] = docv_dz_nom(z_nc); Hv2[:, 1] = Im
        Sv2 = np.einsum('ni,nij,nj->n', Hv2, P_nc, Hv2) + R_U_k2
        Kv2 = np.einsum('nij,nj->ni', P_nc, Hv2)/Sv2[:, None]
        z_nc = np.clip(z_nc + Kv2[:, 0]*yV2, 0.0, 1.0); R0_nc = np.clip(R0_nc + Kv2[:, 1]*yV2, 0.05e-3, 4e-3)
        P_nc -= np.einsum('ni,nj,njk->nik', Kv2, Hv2, P_nc); P_nc = 0.5*(P_nc+P_nc.transpose(0, 2, 1))
        yF2 = Fm - (fb_nc + force_nom(z_nc))
        Hf2 = np.zeros((N_CELLS, 3)); Hf2[:, 0] = dforce_dz_nom(z_nc); Hf2[:, 2] = 1.0
        infl2 = np.clip(np.abs(Hf2[:, 0])/18.0, 0.04, 1.0); Rf2 = (SIG_F**2)/infl2
        P_nc[:, 0, 0] += Q_F_k2
        Sf2 = np.einsum('ni,nij,nj->n', Hf2, P_nc, Hf2) + Rf2
        Kf2 = np.einsum('nij,nj->ni', P_nc, Hf2)/Sf2[:, None]
        z_nc = np.clip(z_nc + Kf2[:, 0]*yF2, 0.0, 1.0); fb_nc += Kf2[:, 2]*yF2
        P_nc -= np.einsum('ni,nj,njk->nik', Kf2, Hf2, P_nc); P_nc = 0.5*(P_nc+P_nc.transpose(0, 2, 1))
        if ica_win[k]:
            Vc2 = Vm - Im*R0_nc
            if np.isnan(Vc_prev_nc[0]): Vc_prev_nc = Vc2.copy()
            else:
                dV2 = Vc2 - Vc_prev_nc; Vc_prev_nc = Vc2.copy()
                ica_val2 = np.abs(Im*DT/3600.0)/(np.abs(dV2)+1e-7)
                ica_buf_nc = np.roll(ica_buf_nc, -1, axis=1); ica_buf_nc[:, -1] = ica_val2
                ica_sm2 = ica_buf_nc.mean(axis=1)
                fired2 = ((ica_sm2 > 8.0) & (k > 100) & (ica_sm2 < ica_prev_nc) &
                           (~ica_done_nc) & (np.abs(docv_dz_nom(z_nc)) > 0.02))
                if fired2.any():
                    ica_fires_nc += int(fired2.sum())
                    z_nc[fired2] = ICA_PEAK_SOC; P_nc[fired2, 0, 0] = 0.02**2; ica_done_nc[fired2] = True
                ica_prev_nc = ica_sm2.copy()
        Znc[k] = z_nc

    k15 = min(int(900/DT), T_STEPS-1)
    err_cl = Zcl-Ztr; err_en = Zen-Ztr; err_nc = Znc-Ztr
    metrics = dict(
        rmse_pack_cl=float(np.sqrt(np.mean((Zcl[k15:].mean(1)-Ztr[k15:].mean(1))**2))),
        rmse_pack_en=float(np.sqrt(np.mean((Zen[k15:].mean(1)-Ztr[k15:].mean(1))**2))),
        rmse_pack_nc=float(np.sqrt(np.mean((Znc[k15:].mean(1)-Ztr[k15:].mean(1))**2))),
        mae_cell_end_cl=float(np.mean(np.abs(err_cl[-1]))),
        mae_cell_end_en=float(np.mean(np.abs(err_en[-1]))),
        mae_cell_end_nc=float(np.mean(np.abs(err_nc[-1]))),
    )
    return metrics


def mc(profile_fn, tag, n=20, seed=0):
    rng = np.random.default_rng(seed)
    keys = ["rmse_pack_cl", "rmse_pack_en", "rmse_pack_nc",
            "mae_cell_end_cl", "mae_cell_end_en", "mae_cell_end_nc"]
    acc = {k: [] for k in keys}
    for j in range(n):
        met = simulate_3way(np.random.default_rng(rng.integers(1 << 31)), profile_fn)
        for k in keys: acc[k].append(met[k])
    return {k: np.array(v) for k, v in acc.items()}


if __name__ == "__main__":
    import sys, os
    tag = sys.argv[1] if len(sys.argv) > 1 else "PS"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    prof = {"PS": m.profile_peak_shaving, "PV": m.profile_pv, "FCR": m.profile_fcr}[tag]
    r = mc(prof, tag, n=n, seed=seed)
    outpath = f"/tmp/noCC_{tag}_{seed}.npz"
    np.savez(outpath, **r)
    # merge with any other seed-batches for this tag already on disk
    merged = {k: [v] for k, v in r.items()}
    for fn in os.listdir("/tmp"):
        if fn.startswith(f"noCC_{tag}_") and fn != os.path.basename(outpath):
            d = dict(np.load(f"/tmp/{fn}"))
            for k in merged: merged[k].append(d[k])
    r = {k: np.concatenate(v) for k, v in merged.items()}
    ntot = len(r["rmse_pack_cl"])

    def q(a): return f"{np.median(a)*100:.2f}  [{np.percentile(a,10)*100:.2f}, {np.percentile(a,90)*100:.2f}]"
    print(f"\n{tag} (n={ntot} total, this batch seed={seed} n={n}):")
    print(f"  pack RMSE  baseline     : {q(r['rmse_pack_cl'])} %")
    print(f"  pack RMSE  enhanced(CC) : {q(r['rmse_pack_en'])} %")
    print(f"  pack RMSE  enhanced(noCC): {q(r['rmse_pack_nc'])} %")
    print(f"  cell MAE@end baseline     : {q(r['mae_cell_end_cl'])} %")
    print(f"  cell MAE@end enhanced(CC) : {q(r['mae_cell_end_en'])} %")
    print(f"  cell MAE@end enhanced(noCC): {q(r['mae_cell_end_nc'])} %")
    nbeats = np.sum(r['rmse_pack_nc'] < r['rmse_pack_en'])
    print(f"  noCC beats CC in {nbeats}/{ntot} draws")
