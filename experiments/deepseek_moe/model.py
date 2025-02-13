from typing import assert_type
import torch
import torch.nn as nn
import torch.nn.functional as F

import math
from dataclasses import dataclass
from ohara.modules.mlp import GLU, MLP,ACT2FN, MLP_MAP
from ohara.modules.norm import RMSNorm

from ohara.embedings_pos.rotatry import precompute_freqs_cis
from ohara.embedings_pos.rotatry import apply_rope

from huggingface_hub import PyTorchModelHubMixin

from collections import OrderedDict

from dsmoe import Config, DSMoE, FFNType, SparseMoE

class Attention(nn.Module):
    def __init__(self, config: Config):
        super().__init__()

        d_model = config.d_model
        self.num_heads = config.num_heads
        self.head_dim = config.head_dim
        self.num_kv_heads = config.num_kv_heads
        
        if self.num_kv_heads == 0:
            self.num_kv_heads = self.num_heads
        else:
            assert self.num_heads % self.num_kv_heads == 0
            self.num_queries_per_kv = self.num_heads // self.num_kv_heads

        self.key = nn.Linear(d_model, self.head_dim * self.num_kv_heads, config.bias)
        self.query = nn.Linear(d_model, self.head_dim * self.num_heads, config.bias)
        self.value = nn.Linear(d_model, self.head_dim * self.num_kv_heads, config.bias)
        self.proj = nn.Linear(self.head_dim * self.num_heads, d_model, config.bias)

        self.attn_dropout = nn.Dropout(config.dropout)
        self.res_dropout = nn.Dropout(config.dropout)

        self.flash_attn = hasattr(torch.nn.functional, "scaled_dot_product_attention") and not config.use_spda

        self.reset_parameters() 
    
    
    def reset_parameters(self, init_std: float | None = None, factor: float = 1.0) -> None:
        init_std = init_std or (self.head_dim ** (-0.5))

        for w in [self.key, self.query, self.value]:
            nn.init.trunc_normal_(
                w.weight,
                mean=0.0,
                std=init_std,
                a=-3 * init_std,
                b=3 * init_std,
            )

        nn.init.trunc_normal_(
            self.proj.weight,
            mean=0.0,
            std=init_std / factor,
            a=-3 * init_std,
            b=3 * init_std,
        )

    def forward(self, x: torch.Tensor, mask: torch.Tensor, freqs_cis) -> torch.Tensor:
        batch, seq_len, d_model = x.shape

        k: torch.Tensor  # type hint for lsp
        q: torch.Tensor  # ignore
        v: torch.Tensor

        k = self.key(x)
        q = self.query(x)
        v = self.value(x)

        k = k.view(
            batch, seq_len, self.num_kv_heads, self.head_dim
        )  # shape = (B, seq_len, num_kv_heads, head_dim)
        q = q.view(batch, seq_len, self.num_heads, self.head_dim)
        v = v.view(batch, seq_len, self.num_kv_heads, self.head_dim)

        q, k = apply_rope(q, k, freqs_cis)

        # Grouped Query Attention
        if self.num_kv_heads != self.num_heads:
            k = torch.repeat_interleave(k, self.num_queries_per_kv, dim=2)
            v = torch.repeat_interleave(v, self.num_queries_per_kv, dim=2)

        k = k.transpose(1, 2)  # shape = (B, num_heads, seq_len, head_dim)
        q = q.transpose(1, 2)
        v = v.transpose(1, 2)

        if self.flash_attn:
            output = torch.nn.functional.scaled_dot_product_attention(
                q,
                k,
                v,  # order important
                attn_mask=None,
                dropout_p=self.attn_dropout.p if self.training else 0.0,
                is_causal=True,
            )
        else:
            attn_mtx = torch.matmul(q, k.transpose(2, 3)) / math.sqrt(self.head_dim)
            attn_mtx = attn_mtx + mask[:, :, :seq_len, :seq_len]
            attn_mtx = F.softmax(attn_mtx.float(), dim=-1).type_as(k)
            attn_mtx = self.attn_dropout(attn_mtx)

            output = torch.matmul(attn_mtx, v)  # (batch, n_head, seq_len, head_dim)

        # restore time as batch dimension and concat heads
        output = output.transpose(1, 2).contiguous().view(batch, seq_len, d_model)

        # final projection into the residual stream
        output = self.proj(output)
        output = self.res_dropout(output)
        return output
    
    


class Block(nn.Module):
    def __init__(self, config: Config, use_dense: bool = False):
        super().__init__()

        self.attn = Attention(config)
        if config.ffn_type == FFNType.Dense or use_dense:
            self.ff:MLP|GLU = MLP_MAP[config.mlp](
                dim=config.d_model,
                hidden_dim=config.hidden_dim,
                activation_fn=config.activation,
            )
        elif config.ffn_type == FFNType.DSMoE:
            self.ff = DSMoE(
                dim=config.d_model,
                hidden_dim=config.hidden_dim,
                num_experts=config.num_experts,
                num_experts_per_tok=config.num_experts_per_tok,
                num_shared_experts=config.num_shared_experts,
                expert_update_rate=config.expert_update_rate,
                aux_free_loadbalancing=config.aux_free_loadbalancing,
                use_aux_loss=config.use_aux_loss,
                train_experts_biases=config.train_experts_biases,
                activation_fn=config.activation,
                mlp=config.mlp,
                dropout=config.dropout,
                bias=config.bias,
            )
        elif config.ffn_type == FFNType.SparseMoE:
            self.ff = SparseMoE(
                dim=config.d_model,
                hidden_dim=config.hidden_dim,
                num_experts=config.num_experts,
                num_experts_per_tok=config.num_experts_per_tok,
                activation_fn=config.activation,
                dropout=config.dropout,
                bias=config.bias,
            )

        self.norm1 = RMSNorm(config.d_model)
        self.norm2 = RMSNorm(config.d_model)

    def forward(self, x, mask, freqs_cis):
        x = x + self.attn(self.norm1(x), mask, freqs_cis)
        x = x + self.ff(self.norm2(x))
        return x
    
    def reset_parameters(self, init_std: float | None = None, factor: float = 1.0) -> None:
        self.attn.reset_parameters(init_std, factor)
        self.ff.reset_parameters(init_std, factor)


class Transformer(nn.Module):
    def __init__(self, config: Config, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.config = config

        self.token_emb = nn.Embedding(config.vocab_size, config.d_model)

        self.layers = nn.ModuleList([Block(config, use_dense=idx < config.dense_layers) for idx in range(config.num_layers)])

        self.norm = RMSNorm(config.d_model)
        self.vocab_proj = nn.Linear(config.d_model, config.vocab_size, bias=False)

        if config.weight_tying:
            self.token_emb.weight = self.vocab_proj.weight

        cos, isin = precompute_freqs_cis(config.d_model // config.num_heads, config.seq_len * 2)
        self.register_buffer("freq_cos", cos)
        self.register_buffer("freq_sin", isin)

        if not hasattr(torch.nn.functional, "scaled_dot_product_attention"):
            print("WARNING: using slow attention | upgrade pytorch to 2.0 or above")
            mask = torch.full((1, 1, config.seq_len, config.seq_len), float("-inf"))
            mask = torch.triu(mask, diagonal=1)
            self.register_buffer("mask", mask)
        else:
            self.mask = None

        self.apply(self._init_weights)
        


    def forward(self, x: torch.Tensor):
        batch, seqlen = x.shape
        x = self.token_emb(x)
        freqs_cis = self.freq_cos[:seqlen], self.freq_sin[:seqlen]

        for layer in self.layers:
            x = layer(x, self.mask, freqs_cis)

        x = self.norm(x)
        x = self.vocab_proj(x)
        return x

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def reset_parameters(self, init_std: float | None = None, factor: float = 1.0) -> None:
        layer:Block
        torch.nn.init.normal_(self.token_emb.weight, mean=0.0, std=0.02)
        torch.nn.init.normal_(self.vocab_proj.weight, mean=0.0, std=0.02)
        for layer in self.layers:
            layer.reset_parameters(init_std, factor)
        self.norm.reset_parameters()

        
        
class ModelingLM(nn.Module, PyTorchModelHubMixin):
    def __init__(self, config: Config, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.config = config
        self.model = Transformer(self.config)
        self.reset_parameters()
        
    def forward(self, x: torch.Tensor, return_outputs: bool = False):
        outputs = self.model(x)
        return outputs

    def reset_parameters(self, init_std: float | None = None, factor: float = 1.0) -> None:
        self.model.reset_parameters(init_std, factor)


if __name__ == "__main__":
    config = Config(
        vocab_size=10,
        seq_len=10,
        d_model=128,
        hidden_dim=128,
        num_heads=4,
        num_kv_heads=0,
        num_layers=4,
        dropout=0.2,
        bias=False,
        weight_tying=False,
        activation="relu_squared",
        mlp="GLU",
    )

    model = ModelingLM(config).eval()
    print(model)
