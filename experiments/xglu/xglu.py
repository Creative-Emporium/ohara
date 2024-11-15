import torch
import torch.nn as nn
import torch.nn.functional as F

from ohara.modules.norm import RMSNorm

from ohara.modules.mlp import ACT2FN

relu_squared = lambda x: F.relu(x)**2

ACT2FN = {
    "relu": F.relu,
    "silu": F.silu,
    "gelu": F.gelu,
    "tanh": torch.tanh,
    "sigmoid": torch.sigmoid,
    "relu_squared": relu_squared,
    "relu2": relu_squared,
}


class XGLU(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int | None = None,
        multiple_of: int = 4,
        dropout: float | None = None,
        activation_fn: str = "silu",
        bias: bool = False,
        *args,
        **kwargs,
    ):
        """
        GLU Variants Improve Transformer
        https://arxiv.org/abs/2002.05202v1

        order in which W1,W2,W3 are multiplied is as per llama (for compatiblity)
        """
        super().__init__()

        if hidden_dim is None:
            hidden_dim = 4 * dim
            hidden_dim = int(2 * hidden_dim / 3)
            hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.up = nn.Linear(dim, hidden_dim, bias=bias)
        self.gate = nn.Linear(dim, hidden_dim, bias=bias)
        self.down = nn.Linear(hidden_dim, dim, bias=bias)
        
        self.gate_norm = RMSNorm(hidden_dim)

        self.activation = ACT2FN[activation_fn]
        self.dropout = nn.Dropout(dropout) if dropout else lambda x: x

    def forward(self, x):
        up = self.up(x)
        gate = self.gate(x)
        gate = self.activation(gate)
        gate = self.gate_norm(gate)
        down = self.down(gate * up)
        return self.dropout(down)
    
class GLU(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int | None = None,
        multiple_of: int = 4,
        dropout: float | None = None,
        activation_fn: str = "silu",
        bias: bool = False,
        *args,
        **kwargs,
    ):
        """
        GLU Variants Improve Transformer
        https://arxiv.org/abs/2002.05202v1

        order in which W1,W2,W3 are multiplied is as per llama (for compatiblity)
        """
        super().__init__()

        if hidden_dim is None:
            hidden_dim = 4 * dim
            hidden_dim = int(2 * hidden_dim / 3)
            hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.up = nn.Linear(dim, hidden_dim, bias=bias)
        self.gate = nn.Linear(dim, hidden_dim, bias=bias)
        self.down = nn.Linear(hidden_dim, dim, bias=bias)

        self.activation = ACT2FN[activation_fn]
        self.dropout = nn.Dropout(dropout) if dropout else lambda x: x

    def forward(self, x):
        up = self.up(x)
        gate = self.gate(x)
        gate = self.activation(gate)
        down = self.down(gate * up)
        return self.dropout(down)

class MLP(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int | None = None,
        multiple_of: int = 4,
        dropout: float | None = None,
        activation_fn: str = "silu",
        bias: bool = True,
    ):
        super().__init__()

        if hidden_dim is None:
            hidden_dim = 4 * dim
            hidden_dim = int(2 * hidden_dim / 3)
            hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.up = nn.Linear(dim, hidden_dim, bias=bias)
        self.down = nn.Linear(hidden_dim, dim, bias=bias)
        self.activation_fn = ACT2FN[activation_fn]

        self.dropout = nn.Dropout(dropout) if dropout else lambda x: x

    def forward(self, x):
        x = self.up(x)
        x = self.activation_fn(x)
        x = self.down(x)
        x = self.dropout(x)
        return x



if __name__ == "__main__":
    dim = 512
    expand_ratio = 4
    x = torch.randn(1, dim)

    glu = GLU(dim=dim, hidden_dim=dim * expand_ratio)
    print(f"glu: {sum(p.numel() for p in glu.parameters()) / 1e6:.2f}M")
    print(f"glu: {glu(x).shape}")

    expand_ratio = 5
    rank = 144
    xglu = XGLU(dim=dim, hidden_dim=dim * expand_ratio)
    print(f"xglu: {sum(p.numel() for p in xglu.parameters()) / 1e6:.2f}M")
    print(f"xglu: {xglu(x).shape}")

    # rank = 256 * 2
    # spring_glu = SpringGlu(
    #     dim=dim, hidden_dim=dim * expand_ratio, expand_ratio=expand_ratio, rank=rank
    # )
    # print(f"spring_glu: {sum(p.numel() for p in spring_glu.parameters()) / 1e6:.2f}M")
    # print(f"spring_glu: {spring_glu(x).shape}")
