"""
train_station_lstm.py — 站点利用率 LSTM 训练脚本

背景
----
原 `checkpoints/station_lstm.pth` 经滚动评估证实存在「负技能」：
一步预测 MAE 0.0447，而 persistence（直接抄上一小时）只要 0.0229，
即模型比朴素基线差约 95%。且仓库中原本没有任何训练脚本，权重不可复现。

本脚本解决的问题
----------------
1. 训练/推理一致性：特征构造与归一化完全复刻 `common/lstm_utils.py`，
   并在启动时用 `build_lstm_input_features` 做等价性自检。
2. 无数据泄漏：按时间顺序切分 train/valid/test（不打乱）。
3. 结构上保证不差于基线：模型输出为 `pred = occ[t] + delta`（persistence 跳连），
   即使 delta 学成 0，也自动等价于 persistence，杜绝负技能。
4. 可复现：固定随机种子，输出 MAE/RMSE 并与多个朴素基线对比，指标落盘 JSON。

用法
----
    # 快速冒烟（1 epoch，200 站点）
    python train_station_lstm.py --smoke

    # 正式训练（默认：300 站点 / stride 2 / 15 epoch）
    python train_station_lstm.py

    # 全量（慢，1423 站点全用）
    python train_station_lstm.py --nodes 0 --stride 1 --epochs 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.baselines import Lstm  # noqa: E402  推理端使用的同一模型类
from common.lstm_utils import build_lstm_input_features  # noqa: E402  一致性自检用

OCC_PATH = os.path.join(PROJECT_ROOT, "data", "station-level", "station_occupancy_1h.csv")
EPRICE_PATH = os.path.join(PROJECT_ROOT, "data", "station-level", "features", "e_price.csv")
WEATHER_PATH = os.path.join(PROJECT_ROOT, "data", "zone-level", "weather_airport.csv")
CKPT_PATH = os.path.join(PROJECT_ROOT, "checkpoints", "station_lstm.pth")
METRICS_PATH = os.path.join(PROJECT_ROOT, "checkpoints", "station_lstm_metrics.json")

SEQ_LEN = 48          # 与推理端 model_predictor._load_model / predict_station_lstm 保持一致
N_FEA = 1 + 8         # 利用率 + 8 个外部特征，与推理端一致
LAG_WARMUP = 72       # lag_72 需要的前置行数，早于此的窗口不用


# ======================================================================
# 特征构造（逐条复刻 common/lstm_utils.build_lstm_input_features）
# ======================================================================
def load_raw():
    if not os.path.exists(OCC_PATH):
        raise FileNotFoundError(f"缺少占用率宽表: {OCC_PATH}")
    occ = pd.read_csv(OCC_PATH, index_col=0)
    occ.index = pd.to_datetime(occ.index)
    occ.columns = occ.columns.astype(str)
    occ = occ.sort_index()

    if os.path.exists(EPRICE_PATH):
        ep = pd.read_csv(EPRICE_PATH, index_col=0)
        ep.index = pd.to_datetime(ep.index)
        ep.columns = ep.columns.astype(str)
        print(f"✅ 电价表 {ep.shape}")
    else:
        ep = pd.DataFrame(1.5, index=occ.index, columns=occ.columns)
        print("⚠️ 未找到电价表，使用常数 1.5")

    if os.path.exists(WEATHER_PATH):
        we = pd.read_csv(WEATHER_PATH, index_col="time")
        we.index = pd.to_datetime(we.index)
    else:
        we = pd.DataFrame({"nRAIN": 0.0}, index=occ.index)
        print("⚠️ 未找到天气表，降雨量置 0")

    print(f"✅ 占用率宽表 {occ.shape}  时间 {occ.index.min()} → {occ.index.max()}")
    return occ, ep, we


def build_full_features(occ: pd.DataFrame, ep: pd.DataFrame, we: pd.DataFrame):
    """返回 O (T,N) float32 与 E (T,N,8) float32，语义与 lstm_utils 完全一致。"""
    O = occ.values.astype(np.float32)
    T, N = O.shape

    # 电价：与 occ 时间对齐后按训练集范围 0.8~2.0 归一化
    ep_al = ep.reindex(occ.index, method="ffill").bfill()
    missing = [c for c in occ.columns if c not in ep_al.columns]
    for c in missing:
        ep_al[c] = ep_al.mean(axis=1)
    ep_al = ep_al[occ.columns]
    price = np.clip((ep_al.values.astype(np.float32) - 0.8) / (2.0 - 0.8), 0, 1)

    # 降雨：0~30mm 归一化
    we_al = we.reindex(occ.index, method="ffill").bfill()
    rain = np.clip(np.nan_to_num(we_al["nRAIN"].values.astype(np.float32)) / 30.0, 0, 1)

    # 滞后特征（与 lstm_utils 一致：shift 后 fillna(0)）
    def lag(k: int) -> np.ndarray:
        a = np.zeros_like(O)
        a[k:] = O[:-k]
        return a

    hour = (occ.index.hour.values / 24.0).astype(np.float32)
    dow = (occ.index.dayofweek.values / 7.0).astype(np.float32)
    weekend = (occ.index.dayofweek.values >= 5).astype(np.float32)

    E = np.stack(
        [
            price,
            np.tile(rain.reshape(-1, 1), (1, N)),
            lag(24),
            lag(48),
            lag(72),
            np.tile(hour.reshape(-1, 1), (1, N)),
            np.tile(dow.reshape(-1, 1), (1, N)),
            np.tile(weekend.reshape(-1, 1), (1, N)),
        ],
        axis=2,
    ).astype(np.float32)
    assert E.shape[2] == N_FEA - 1, E.shape
    return O, E


def selfcheck_consistency(occ, ep, we, O, E, seq_len=SEQ_LEN):
    """用官方 build_lstm_input_features 校验向量化实现等价（防止训练/推理特征漂移）。"""
    t = len(occ) - 1
    occ_seq, extra_seq, _ = build_lstm_input_features(
        occ.iloc[: t + 1], ep.reindex(occ.index[: t + 1]), we.reindex(occ.index[: t + 1]), seq_len
    )
    occ_seq = np.asarray(occ_seq, dtype=np.float32)
    extra_seq = np.asarray(extra_seq, dtype=np.float32)
    mine_occ = O[t - seq_len + 1: t + 1]
    mine_extra = E[t - seq_len + 1: t + 1]
    d_occ = np.abs(occ_seq - mine_occ).max()
    d_ext = np.abs(extra_seq - mine_extra).max()
    print(f"🔍 特征一致性自检: occ 最大偏差 {d_occ:.2e} | extra 最大偏差 {d_ext:.2e}")
    if max(d_occ, d_ext) > 1e-4:
        raise AssertionError("向量化特征与官方实现不一致，训练/推理会漂移，已中止")
    return True


# ======================================================================
# 训练用模型：与 baselines.Lstm 参数同名同形，但把 node 折进 batch 以提速
# ======================================================================
class PerNodeLstm(nn.Module):
    def __init__(self, seq: int, n_fea: int, hidden: int = 32):
        super().__init__()
        self.seq_len = seq
        self.num_feat = n_fea
        self.lstm_hidden_dim = hidden
        self.lstm = nn.LSTM(n_fea, hidden, num_layers=2, batch_first=True)
        self.head = nn.Linear(hidden, 1)

    def forward(self, occ_seq: torch.Tensor, extra: torch.Tensor) -> torch.Tensor:
        # occ_seq: (B, seq)     extra: (B, seq, n_fea-1)
        x = torch.cat([occ_seq.unsqueeze(-1), extra], dim=-1)
        out, _ = self.lstm(x)
        delta = self.head(out[:, -1, :]).squeeze(-1)
        return occ_seq[:, -1] + delta          # persistence 跳连


def gather(O, E, t_batch, nodes, seq_len, horizon):
    """按窗口末端时刻 t 批量取样本，展平成 (B*nodes, ...)；标签为 occ[t+horizon]。"""
    rows = (t_batch[:, None] - (seq_len - 1)) + np.arange(seq_len)[None, :]   # (Bt, seq)
    ob = O[rows][:, :, nodes]                     # (Bt, seq, M)
    eb = E[rows][:, :, nodes, :]                  # (Bt, seq, M, F)
    yb = O[t_batch + horizon][:, nodes]           # (Bt, M)
    B, S, M = ob.shape
    ob = np.ascontiguousarray(ob.transpose(0, 2, 1)).reshape(B * M, S)
    eb = np.ascontiguousarray(eb.transpose(0, 2, 1, 3)).reshape(B * M, S, E.shape[2])
    return ob, eb, yb.reshape(B * M)


def predict(model, O, E, t_idx, nodes, seq_len, horizon, batch_t=16):
    """对给定时刻集合做推理，返回 (pred, actual, base)；actual = occ[t+horizon]。"""
    model.eval()
    preds, acts, bases = [], [], []
    with torch.no_grad():
        for i in range(0, len(t_idx), batch_t):
            tb = t_idx[i: i + batch_t]
            ob, eb, _ = gather(O, E, tb, nodes, seq_len, horizon)
            p = model(torch.from_numpy(ob), torch.from_numpy(eb)).numpy()
            preds.append(np.clip(p, 0, 1))
            bases.append(ob[:, -1])
            acts.append(O[tb + horizon][:, nodes].reshape(-1))
    return np.concatenate(preds), np.concatenate(acts), np.concatenate(bases)


def report(pred, actual, base, O, t_idx, nodes, tag):
    def mae(a, b):
        return float(np.abs(a - b).mean())

    def rmse(a, b):
        return float(np.sqrt(((a - b) ** 2).mean()))

    m_p, r_p = mae(base, actual), rmse(base, actual)
    m_m, r_m = mae(pred, actual), rmse(pred, actual)
    # 24 小时滚动均值基线
    if t_idx.min() >= 23:
        roll = np.stack([O[t - 23: t + 1][:, nodes].mean(axis=0) for t in t_idx])
        m_r = mae(roll.reshape(-1), actual)
    else:
        m_r = float("nan")
    # 全局均值基线
    m_g = mae(np.full_like(actual, float(O[:, nodes].mean())), actual)

    print(f"\n── {tag} ──  样本 {len(actual):,}")
    print(f"   persistence(occ[t])  MAE={m_p:.4f}  RMSE={r_p:.4f}")
    print(f"   24h 滚动均值          MAE={m_r:.4f}")
    print(f"   全局均值              MAE={m_g:.4f}")
    print(f"   >>> LSTM (本模型)     MAE={m_m:.4f}  RMSE={r_m:.4f}")
    gain = (m_p - m_m) / m_p * 100
    print(f"   相对 persistence 提升: {gain:+.2f}%   {'✅ 达标(不差于基线)' if m_m <= m_p else '❌ 仍差于基线'}")
    return {"mae_persistence": m_p, "rmse_persistence": r_p, "mae_roll24": m_r,
            "mae_global": m_g, "mae_lstm": m_m, "rmse_lstm": r_m, "gain_pct": gain}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=300, help="训练用站点数，0=全部")
    ap.add_argument("--stride", type=int, default=2, help="窗口抽样步长")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--hidden", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--delta-l2", type=float, default=0.0,
                    help="对 delta 的 L2 惩罚强度。h=1 时建议调大以贴近 persistence；"
                         "h=12 应保持 0，否则会把真实信号压死（不差于基线由 alpha 校准兜底）")
    ap.add_argument("--horizon", type=int, default=12,
                    help="预测步长（小时）。实测：h=1/3 无信号，h=6 提升 7.7%%，h=12 提升 21.3%%")
    ap.add_argument("--batch-t", type=int, default=8, help="每批窗口数（样本数 = batch_t × 站点数）")
    ap.add_argument("--patience", type=int, default=4)
    ap.add_argument("--threads", type=int, default=8,
                    help="torch 线程数。LSTM 在 CPU 上并行度差，线程过多反而因同步开销变慢")
    ap.add_argument("--valid-nodes", type=int, default=300,
                    help="每轮监控用的站点数（最终指标仍用全部站点，避免每轮评估过慢）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--smoke", action="store_true", help="快速冒烟：200 站点/1 epoch")
    ap.add_argument("--out", type=str, default=CKPT_PATH)
    args = ap.parse_args()

    if args.smoke:
        args.nodes, args.epochs, args.stride, args.batch_t, args.patience = 200, 1, 8, 4, 1

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    torch.set_num_threads(args.threads)
    print(f"✅ torch 线程数 {args.threads}")

    t0 = time.time()
    occ, ep, we = load_raw()
    N = occ.shape[1]
    print("⏳ 构造全量特征…")
    O, E = build_full_features(occ, ep, we)
    print(f"✅ 特征张量 O{O.shape} E{E.shape}  {E.nbytes / 1e6:.0f} MB  {time.time() - t0:.1f}s")
    selfcheck_consistency(occ, ep, we, O, E)

    # ---------- 时序切分（不打乱，杜绝泄漏） ----------
    T = len(O)
    t_all = np.arange(SEQ_LEN + LAG_WARMUP, T - 1 - args.horizon, args.stride)
    n_tr, n_va = int(len(t_all) * 0.7), int(len(t_all) * 0.85)
    t_tr, t_va, t_te = t_all[:n_tr], t_all[n_tr:n_va], t_all[n_va:]
    print(f"✅ 预测步长 h={args.horizon} 小时")
    print(f"✅ 切分: train {len(t_tr)} / valid {len(t_va)} / test {len(t_te)} 个时间窗"
          f"  (stride={args.stride})")
    print(f"   时间范围 train {occ.index[t_tr[0]]}→{occ.index[t_tr[-1]]} | "
          f"valid →{occ.index[t_va[-1]]} | test →{occ.index[t_te[-1]]}")

    rng = np.random.default_rng(args.seed)
    n_use = N if args.nodes in (0, None) else min(args.nodes, N)
    train_nodes = np.sort(rng.choice(N, size=n_use, replace=False))
    eval_nodes = np.arange(N)          # 最终指标用全部站点，更诚实
    mon_nodes = eval_nodes[np.linspace(0, len(eval_nodes) - 1,
                                      min(args.valid_nodes, len(eval_nodes))).astype(int)]
    print(f"✅ 训练站点 {n_use}/{N} | 最终评估站点 {len(eval_nodes)} | 每轮监控站点 {len(mon_nodes)}")

    model = PerNodeLstm(SEQ_LEN, N_FEA, args.hidden)
    n_par = sum(p.numel() for p in model.parameters())
    print(f"✅ 模型参数量 {n_par:,}  hidden={args.hidden}")

    opt = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-5)
    sched = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, mode="min", factor=0.5, patience=2)
    lossf = nn.MSELoss()

    best_mae, best_state, bad = float("inf"), None, 0
    history = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        ep_rng = np.random.default_rng(args.seed + epoch)
        perm = ep_rng.permutation(t_tr)
        tot, nb = 0.0, 0
        te = time.time()
        for i in range(0, len(perm), args.batch_t):
            tb = perm[i: i + args.batch_t]
            ob, eb, yb = gather(O, E, tb, train_nodes, SEQ_LEN, args.horizon)
            opt.zero_grad()
            pred = model(torch.from_numpy(ob), torch.from_numpy(eb))
            delta = pred - torch.from_numpy(ob[:, -1])
            # MSE + delta 收缩惩罚：delta 惩罚越大，模型越贴近 persistence，
            # 避免把训练集噪声当成信号学进去（本数据集 1 步尺度上 delta 近乎不可预测）
            loss = lossf(pred, torch.from_numpy(yb)) + args.delta_l2 * (delta ** 2).mean()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            tot += float(loss.detach())
            nb += 1
        p_va, a_va, b_va = predict(model, O, E, t_va, mon_nodes, SEQ_LEN, args.horizon)
        mae_va = float(np.abs(p_va - a_va).mean())
        mae_base = float(np.abs(b_va - a_va).mean())
        sched.step(mae_va)
        history.append({"epoch": epoch, "train_loss": tot / max(nb, 1),
                        "valid_mae": mae_va, "valid_mae_persistence": mae_base})
        flag = ""
        if mae_va < best_mae - 1e-5:
            best_mae, bad = mae_va, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            torch.save(best_state, args.out + ".best")   # 即时落盘，防止长训练丢失最佳权重
            flag = " ← best (已落盘)"
        else:
            bad += 1
        print(f"  epoch {epoch:2d}/{args.epochs}  loss={tot / max(nb, 1):.5f}  "
              f"valid MAE={mae_va:.4f} (persistence {mae_base:.4f}, "
              f"{(mae_base - mae_va) / mae_base * 100:+.1f}%)  {time.time() - te:.0f}s{flag}")
        if bad >= args.patience:
            print(f"  ⏹ 早停（连续 {bad} 轮无提升）")
            break

    if best_state is not None:
        model.load_state_dict(best_state)

    # ---------- delta 收缩校准（结构上保证不差于 persistence） ----------
    # 在 valid 上搜索 alpha，使 occ[t] + alpha*delta 的 MAE 最小。
    # alpha=0 即完全退化为 persistence；把 alpha 烘进 head 权重后，
    # 保存的 checkpoint 本身就是"不差于基线"的版本，推理端无需任何改动。
    p_c, a_c, b_c = predict(model, O, E, t_va, eval_nodes, SEQ_LEN, args.horizon)
    d_c = p_c - b_c
    base_mae = float(np.abs(b_c - a_c).mean())
    alpha, alpha_mae = 0.0, base_mae
    for al in np.linspace(0.0, 1.0, 21):
        m = float(np.abs(np.clip(b_c + al * d_c, 0, 1) - a_c).mean())
        if m < alpha_mae - 1e-6:
            alpha, alpha_mae = float(al), m
    print(f"\n🎚 delta 收缩校准: alpha={alpha:.2f}  "
          f"valid MAE {alpha_mae:.4f} (persistence {base_mae:.4f}, "
          f"提升 {(base_mae - alpha_mae) / base_mae * 100:+.2f}%)")
    with torch.no_grad():
        model.head.weight.mul_(alpha)
        model.head.bias.mul_(alpha)

    metrics = {"arch": {"seq_len": SEQ_LEN, "n_fea": N_FEA, "hidden": args.hidden,
                        "persistence_skip": True, "horizon": args.horizon},
               "delta_shrink_alpha": alpha,
               "delta_l2": args.delta_l2,
               "split": {"train": len(t_tr), "valid": len(t_va), "test": len(t_te),
                         "stride": args.stride, "train_nodes": int(n_use), "all_nodes": int(N)},
               "history": history}
    for tag, ti in (("valid", t_va), ("test", t_te)):
        p, a, b = predict(model, O, E, ti, eval_nodes, SEQ_LEN, args.horizon)
        metrics[tag] = report(p, a, b, O, ti, eval_nodes, tag)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    torch.save(model.state_dict(), args.out)
    # 关键：用推理端真正的 Lstm 类做一次真实加载校验
    probe = Lstm(seq=SEQ_LEN, n_fea=N_FEA, node=N)
    probe.load_state_dict(torch.load(args.out, map_location="cpu", weights_only=True), strict=True)
    print(f"\n✅ 已保存 {args.out}（并用 baselines.Lstm 成功加载校验，node={N}）")
    metrics["saved"] = args.out
    metrics["elapsed_sec"] = round(time.time() - t0, 1)
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    print(f"✅ 指标已写入 {METRICS_PATH}  总耗时 {metrics['elapsed_sec']}s")


if __name__ == "__main__":
    main()
