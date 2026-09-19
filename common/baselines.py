import os

import torch
import torch.nn as nn
import numpy as np
# from torch_geometric_temporal.nn.attention.astgcn import ASTGCN
from statsmodels.tsa.ar_model import AutoReg
from statsmodels.tsa.arima.model import ARIMA
import warnings


class Lo:
    def __init__(self, args):
        self.args = args
        self.pred_len = args.pred_len

    def predict(self, train_valid_occ, test_occ):
        """
        Use the latest observed value as the prediction for the next time step.
        """
        time_len, node = test_occ.shape
        preds = np.zeros((time_len, node))

        for j in range(node):
            for i in range(time_len):
                if i < self.pred_len:
                    preds[i, j] = train_valid_occ[-self.pred_len + i, j]
                else:
                    preds[i, j] = test_occ[i - self.pred_len, j]

        return preds


class Ar:
    def __init__(self, pred_len, args, lags=1):
        """
        Initialize the AR model parameters.

        Args:
            lags (int): The number of lagged observations to use in the model.
        """
        self.args = args
        self.pred_len = pred_len
        self.lags = lags

    def predict(self, train_valid_occ, test_occ):
        time_len, node = test_occ.shape
        train_valid_occ = train_valid_occ[:-self.pred_len, :]
        preds = np.zeros((time_len, node))

        for j in range(node):
            series = list(train_valid_occ[:, j])
            for i in range(time_len):
                try:
                    model = AutoReg(series, lags=self.lags).fit()
                    pred = model.predict(start=len(series), end=len(series))
                    preds[i, j] = pred[0]
                    series.append(pred[0])  # 滚动加入预测值
                except Exception as e:
                    print(f"[AutoReg] Failed at node {j}, step {i}: {e}")
                    preds[i, j] = np.nan

        return preds


class Arima:
    def __init__(self, pred_len, args, p=1, d=1, q=1):
        self.pred_len = pred_len
        self.args = args
        self.p = p
        self.d = d
        self.q = q

    def predict(self, train_valid_occ, test_occ):
        time_len, node = test_occ.shape
        train_valid_occ = train_valid_occ[:-self.pred_len, :]
        preds = np.zeros((time_len, node))

        warnings.filterwarnings("ignore")

        for j in range(node):
            fit_series = train_valid_occ[:, j]
            try:
                model = ARIMA(fit_series, order=(self.p, self.d, self.q))
                model_fitted = model.fit()
                # Predict future time steps (equal to time_len)
                pred = model_fitted.forecast(steps=time_len)
                preds[:, j] = pred
            except Exception as e:
                print(f"ARIMA failed for node {j}: {e}")
                preds[:, j] = np.nan  # or use zeros

        return preds


class Fcnn(nn.Module):
    def __init__(self, n_fea, node=247, seq=12):  # input_dim = seq_length
        super(Fcnn, self).__init__()
        self.num_feat = n_fea
        self.seq = seq
        self.nodes = node
        self.linear = nn.Linear(seq * n_fea, 1)

    def forward(self, occ, extra_feat=None):
        if extra_feat is not None and extra_feat != 'None':
            x = torch.cat([occ.unsqueeze(-1), extra_feat], dim=-1)
        else:
            x = occ.unsqueeze(-1)

        batch_size, nodes, seq_len, num_feat = x.shape
        assert nodes == self.nodes
        x = x.view(-1, self.nodes, self.seq * self.num_feat)
        x = self.linear(x)
        x = torch.squeeze(x)
        return x


class Lstm(nn.Module):
    """
    站点级 LSTM 预测器（含 persistence 跳连）。

    设计要点：
      1. 每个站点独立经过同一个共享 LSTM（参数跨站点共享，无 node embedding）。
      2. 输出头只吃「最后一步隐状态」，而不是把整段序列展平后接一个大 Linear。
      3. 关键：pred = occ[t] + delta，即「上一小时实测值 + 模型修正量」。
         这样即使模型什么都没学到（delta≈0），输出也自动等价于 persistence 基线，
         从结构上杜绝「比朴素基线还差」的负技能。
    """

    def __init__(self, seq, n_fea, node=331, hidden=32):
        super(Lstm, self).__init__()
        self.num_feat = n_fea
        self.nodes = node
        self.seq_len = seq
        self.lstm_hidden_dim = hidden
        self.lstm = nn.LSTM(input_size=n_fea, hidden_size=hidden, num_layers=2,
                            batch_first=True)
        self.head = nn.Linear(hidden, 1)

    def forward(self, occ, extra_feat=None):  # occ.shape = [batch, node, seq]
        if extra_feat is not None and extra_feat != 'None':
            x = torch.cat([occ.unsqueeze(-1), extra_feat], dim=-1)
        else:
            x = occ.unsqueeze(-1)

        bs = x.shape[0]
        x = x.view(bs * self.nodes, self.seq_len, self.num_feat)

        lstm_out, _ = self.lstm(x)  # (bs * node, seq_len, hidden)
        last = lstm_out[:, -1, :].view(bs, self.nodes, self.lstm_hidden_dim)
        delta = self.head(last).squeeze(-1)  # (bs, node) 预测修正量
        pred = occ[:, :, -1] + delta         # persistence 跳连
        # 推理时 batch=1，返回 (node,)；用显式取 [0] 而不是 squeeze，
        # 否则 node=1（单站点预测）时会被压成 0 维张量
        return pred[0] if pred.shape[0] == 1 else pred


class Gcn(nn.Module):
    def __init__(self, seq, n_fea, adj_dense, gcn_hidden=32, gcn_layers=2):
        super(Gcn, self).__init__()
        self.nodes = adj_dense.shape[0]
        self.gcn_hidden = gcn_hidden
        self.gcn_layers = gcn_layers
        self.num_feat = n_fea
        self.act = nn.ReLU()
        self.encoder = nn.Conv2d(self.nodes, self.nodes, (1, n_fea))

        # Calculate A_delta matrix (normalized adjacency)
        deg = torch.sum(adj_dense, dim=0)
        deg = torch.diag(deg)
        deg_delta = torch.linalg.inv(torch.sqrt(deg))
        a_delta = torch.matmul(torch.matmul(deg_delta, adj_dense), deg_delta)
        self.A = a_delta

        # Define GCN layers
        self.gcn_layers_list = nn.ModuleList()
        self.gcn_layers_list.append(nn.Linear(seq, self.gcn_hidden))  # Input to first GCN layer
        for _ in range(self.gcn_layers - 1):
            self.gcn_layers_list.append(nn.Linear(self.gcn_hidden, self.gcn_hidden))

        self.decoder = nn.Linear(self.gcn_hidden + seq, 1)

    def forward(self, occ, extra_feat=None):
        if extra_feat is not None and extra_feat != 'None':
            x = torch.cat([occ.unsqueeze(-1), extra_feat], dim=-1)
        else:
            x = occ.unsqueeze(-1)

        batch_size, nodes, seq_len, num_feat = x.shape
        assert nodes == self.nodes

        # GCN forward pass
        x = self.encoder(x)
        gcn_out = x.view(batch_size, nodes, -1)  # Flatten sequence and features into [batch, node, seq * n_fea]

        for gcn_layer in self.gcn_layers_list:
            gcn_out = gcn_layer(gcn_out)
            gcn_out = torch.einsum('ij,bjh->bih', self.A, gcn_out)
            gcn_out = self.act(gcn_out)

        combined_out = torch.cat((occ, gcn_out), dim=-1)
        x = self.decoder(combined_out)
        x = torch.squeeze(x)

        return x


class Gcnlstm(nn.Module):
    def __init__(self, seq, n_fea, adj_dense, node=307, gcn_out=32, gcn_layers=1, lstm_hidden_dim=256, lstm_layers=2,
                 hidden_dim=32):
        super(Gcnlstm, self).__init__()

        self.nodes = node
        self.seq_len = seq
        self.num_feat = n_fea
        self.lstm_hidden_dim = lstm_hidden_dim
        self.lstm_layers = lstm_layers
        self.gcn_out = gcn_out
        self.gcn_layers = gcn_layers
        self.hidden_dim = hidden_dim

        # Initialize GCN layers
        self.gcn_layers_list = nn.ModuleList()
        for i in range(gcn_layers):
            in_dim = seq * n_fea if i == 0 else gcn_out
            self.gcn_layers_list.append(nn.Linear(in_dim, gcn_out))
        self.act = nn.ReLU()

        # Initialize LSTM layer
        self.lstm = nn.LSTM(input_size=n_fea, hidden_size=self.lstm_hidden_dim, num_layers=self.lstm_layers,
                            batch_first=True)
        self.decoder = nn.Linear(1 + self.lstm_hidden_dim + self.gcn_out, 1)  # occ最后一步 + lstm + gcn

        # Calculate A_delta matrix
        deg = torch.sum(adj_dense, dim=0)
        deg = torch.diag(deg)
        deg_delta = torch.linalg.inv(torch.sqrt(deg))
        a_delta = torch.matmul(torch.matmul(deg_delta, adj_dense), deg_delta)
        self.A = a_delta

    def forward(self, occ, extra_feat=None):
        # 拼接特征
        if extra_feat is not None and extra_feat != 'None':
            x = torch.cat([occ.unsqueeze(-1), extra_feat], dim=-1)
        else:
            x = occ.unsqueeze(-1)

        batch_size, nodes, seq_len, num_feat = x.shape
        assert nodes == self.nodes, f"Nodes mismatch: {nodes} != {self.nodes}"

        # LSTM 分支：处理每个节点的序列
        x_lstm = x.view(batch_size * nodes, seq_len, num_feat)
        lstm_out, _ = self.lstm(x_lstm)  # (batch * nodes, seq_len, lstm_hidden_dim)
        lstm_out = lstm_out.view(batch_size, nodes, seq_len, self.lstm_hidden_dim)
        lstm_feat = lstm_out[:, :, -1, :]  # (batch, nodes, lstm_hidden_dim)

        # GCN 分支：将 (seq * num_feat) 映射到 gcn_out，再进行图卷积
        x_gcn = x.view(batch_size, nodes, seq_len * num_feat)
        for gcn_layer in self.gcn_layers_list:
            x_gcn = gcn_layer(x_gcn)  # (batch, nodes, gcn_out)
            x_gcn = torch.einsum('ij,bjh->bih', self.A, x_gcn)
            x_gcn = self.act(x_gcn)

        # 融合：occ 最后一步 + lstm 最后步 + gcn_out
        occ_feat = occ[:, :, -1:]  # (batch, nodes, 1)
        combined = torch.cat([occ_feat, lstm_feat, x_gcn], dim=-1)  # (batch, nodes, 1 + lstm_hidden_dim + gcn_out)

        out = self.decoder(combined)  # (batch, nodes, 1)
        out = torch.squeeze(out)  # (batch, nodes)

        return out