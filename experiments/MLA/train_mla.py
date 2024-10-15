import sys
import time
import math
from datetime import datetime
import os

from typing import Any, Dict, Callable
from dataclasses import dataclass, field


import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch import Tensor

from ohara.lr_scheduler import CosineScheduler
from ohara.dataset import PreTokenizedDataset
from ohara.utils import BetterCycle

from ohara.utils import auto_accelerator, random_name, model_summary


from torch.utils.data import DataLoader
from transformers import AutoTokenizer


from rich import print, traceback

import lightning as L
from lightning.pytorch.loggers import WandbLogger
from lightning.fabric.loggers import TensorBoardLogger

from modeling_mla import ModelingLM, Config
from ohara.trainer import Trainer
attn_type = "mla"

traceback.install()

torch.set_float32_matmul_precision("high")

# ================================================================================================

### CONFIGS
wandb_project_name = "Ohara-MODEL"
wandb_run_name = random_name()

learning_rate: float = 5e-4
min_learning_rate: float = 0.0

max_iters: int = 10_000
warmup_iters: int = max_iters//10 

total_batch_size:int = 2**13 
seq_len: int = 256
batch_size: int = 8 
micro_batch: int = int(total_batch_size/(seq_len*batch_size))
eval_iters: int = 100
save_ckpt_iters: int = 2000

multiple_of: int = 4
d_model: int = 1024 
hidden_dim = int(d_model * multiple_of)
num_layers: int = 10 #32 // 3  # 44
num_heads: int = 10

kv_lora_rank = 64
q_lora_rank = 3 * kv_lora_rank

head_dim: int = 128
rope_head_dim: int = 32

mlp: str = "GLU"
activation_fn: str = "silu"

MONKEY_PATCH = False
model_name = f"joey00072/model_name_{attn_type}"

assert d_model % num_heads == 0

compile_model = True# not bool(sys.platform == "darwin")
compile_mode:str = None #"reduce-overhead"

### Dataset and Tokenizer
dataset_name = "joey00072/pretokenized__JeanKaddour_minipile__EleutherAI__gpt-neo-125m"  # run pretokeinze first
tokenizer_name = "EleutherAI/gpt-neo-125m"

### Setup
device = auto_accelerator()  # auto chose device (cuda, mps)

# for restarting training from last checkpoint
resume_training = False
push_to_hub = False
checkpoint_path = "./ckpt/model.pt"

wandb_logger = False
tensorboard_logger = True

print(f">>>> {attn_type} <<<<")
# ================================================================================================


def main():
    hyper_params = {
        "learning_rate": learning_rate,
        "min_learning_rate": min_learning_rate,
        "warmup_iters": warmup_iters,
        "max_iters": max_iters,
        "eval_iters": eval_iters,
        "batch_size": batch_size,
        "micro_batch": micro_batch,
        "d_model": d_model,
        "seq_len": seq_len,
        "num_layers": num_layers,
        "num_heads": num_heads,
        "multiple_of": multiple_of,
        "compile_model": compile_model,
        "tokenizer_name": tokenizer_name,
        "dataset_name": dataset_name,
        "resume_training": resume_training,
        "save_ckpt_iters": save_ckpt_iters,
        "attn_type": attn_type,
        "head_dim": head_dim,
        "rope_head_dim": rope_head_dim,
        "kv_lora_rank": kv_lora_rank,
        "q_lora_rank": q_lora_rank,
    }
    
    loggers = []

    if wandb_logger:
        logger = WandbLogger(project=wandb_project_name, resume=resume_training)
        loggers.append(logger)

    if tensorboard_logger:
        logger = TensorBoardLogger(root_dir="./logs", name=wandb_project_name)
        loggers.append(logger)

    # fabric init
    fabric = L.Fabric(loggers=loggers, precision="bf16-mixed")
    fabric.logger.log_hyperparams(hyper_params)

    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)

    config = Config(
        vocab_size=tokenizer.vocab_size,
        seq_len=seq_len,
        d_model=d_model,
        hidden_dim=hidden_dim,
        head_dim= head_dim,
        rope_head_dim=rope_head_dim,
        num_heads=num_heads,
        num_kv_heads=0,
        num_layers=num_layers,
        dropout=0.2,
        bias=False,
        weight_tying=True,
        activation=activation_fn,
        mlp=mlp,
        attn_type=attn_type,
        kv_lora_rank=kv_lora_rank,
        q_lora_rank=q_lora_rank,
    )

    train_ds = PreTokenizedDataset(
        dataset_name=dataset_name,
        tokenizer=tokenizer,
        split="train",
        hf=True,
        max_length=seq_len,
    )
    test_ds = PreTokenizedDataset(
        dataset_name=dataset_name,
        tokenizer=tokenizer,
        split="validation",
        hf=True,
        max_length=seq_len,
    )

    train_dataloader = DataLoader(train_ds, batch_size=batch_size)
    test_dataloader = DataLoader(test_ds, batch_size=batch_size)
    train_dataloader, test_dataloader = fabric.setup_dataloaders(train_dataloader, test_dataloader)

    model = ModelingLM(config)

    if MONKEY_PATCH:
        target_layers = ["key", "value", "query", "proj", "w1", "w2", "w3"]
        dm_linear_monkey_patch(model, target_layers)
        print("\n>>>> Applied monkey patching <<<<\n")
        
    model: L.LightningModule = fabric.setup(model)
    print("=" * 100)
    if compile_model:
        model = torch.compile(model,mode=compile_mode)
    # model.gradient_checkpointing_enable()
    print(model)
    print(model_summary(model.model.layers[0]))
    import time

    get_lr = CosineScheduler(
        learning_rate=learning_rate,
        min_lr=min_learning_rate,
        warmup_iters=warmup_iters,
        max_iters=max_iters,
    )

    # inputs = torch.tensor(tokenizer.encode("The")).unsqueeze(0).clone().detach()
    optimizer = optim.AdamW(model.parameters(), lr=get_lr(0))
    optimizer = fabric.setup_optimizers(optimizer)

    trainer = Trainer(
        fabric=fabric,
        model=model,
        optimizer=optimizer,
        train_dataloader=train_dataloader,
        val_dataloader=test_dataloader,
        get_lr=get_lr,
        micro_batch=micro_batch,
        max_iters=max_iters,
        eval_iters=eval_iters,
        save_ckpt_iters=save_ckpt_iters,
        ignore_index=-1,
        push_to_hub=push_to_hub,
        model_name=model_name,
    )

    start_iter = 0
    if resume_training and os.path.exists(checkpoint_path):
        print(f"Resuming from checkpoint: {checkpoint_path}")
        checkpoint = fabric.load(checkpoint_path)
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        start_iter = checkpoint['idx']
        print(f"Resuming from iteration: {start_iter}")

    # Let's GO!!
    trainer.train(start_iter=start_iter)

    # TODO: Replace this inference
    start: float = time.time()
    max_tokens = 200
    model = model.eval()
    inputs = tokenizer.encode("Once")
    with torch.no_grad():
        inputs = torch.tensor(inputs).reshape(1, -1).to(fabric.device).clone().detach()
        for _idx in range(max_tokens):
            logits = model(inputs)
            max_logits = torch.argmax(logits, dim=-1)
            inputs: torch.Tensor = torch.cat((inputs, max_logits[:, -1:]), dim=-1)
            print(tokenizer.decode(inputs.tolist()[0][-1]), end="", flush=True)

    end: float = time.time()

    print(f"Time: {end-start}")


if __name__ == "__main__":
    main()
