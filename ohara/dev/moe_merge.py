import torch 
import torch.nn as nn
import torch.nn.functional as F
from pprint import pprint
from safetensors import safe_open
from typing import Tuple

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def svd_approx(W: torch.Tensor, r: int = None) -> Tuple[torch.Tensor, torch.Tensor, int, torch.Tensor]:
    """
    Perform SVD approximation of a matrix with an optional rank specification.
    
    Parameters:
    W (torch.Tensor): The input matrix to be approximated.
    r (int, optional): The rank for the approximation. If None, the full rank is used.

    Returns:
    Tuple[torch.Tensor, torch.Tensor, int, torch.Tensor]: 
        A tuple containing the matrices A and B (where A @ B is the approximation of W), 
        the rank used for the approximation, and the Frobenius norm of the difference between W and its approximation.
    """
    U, S, V = torch.linalg.svd(W, full_matrices=False)
    if r is None:
        r = S.size(0)
    else:
        r = min(r, S.size(0))

    A = U[:, :r]
    B = torch.diag(S[:r]) @ V[:r, :]
    approx_W = A @ B
    frobenius_norm = torch.linalg.norm(W - approx_W)
    return A, B, r, frobenius_norm


path = "model-00015-of-00019.safetensors"
tensors = {}
with safe_open(path, framework="pt") as f:
    for k in f.keys():
        tensors[k] = f.get_tensor(k)
        
pprint(tensors.keys())
print("-"*100)

layer = 24
weight_name = "w1"
ws:list[torch.Tensor]=[]

for k,v in tensors.items():
    if "24" in k and weight_name in k:
        ws.append(v)
del tensors

dim = min(ws[0].float().numpy().shape)
experts = 8
r = dim//experts

print(f"experts: {experts}, dim: {dim}, r: {r}")

As = []
Bs = []
for w in ws:
    _,dim = w.shape
    w = w.float().to(device)
    A, B, r, _ = svd_approx(w, r)
    As.append(A)
    Bs.append(B)
    

Wp = torch.zeros_like(ws[0])

for i in range(experts):
    Wd = As[i] @ Bs[i]
    Wp += ws[i] - Wd
Wp = Wp/experts

for idx,W,A,B in enumerate(zip(ws,As,Bs)):
    Wapx = Wp + A @ B
    print(idx,torch.linalg.norm(W - Wapx))