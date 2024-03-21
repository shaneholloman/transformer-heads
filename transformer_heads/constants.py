"""
This module defines constants for loss functions and model types.

It includes `loss_fct_map`, a dictionary that maps loss function names to their corresponding PyTorch implementations, 
and `model_type_map`, a dictionary that maps model type names to their corresponding transformers model classes.
activation_map is a dictionary that maps activation function names to their corresponding PyTorch implementations.
"""

import torch.nn as nn
from transformers import GPT2Model, LlamaModel, MistralModel

activation_map = {"sigmoid": nn.Sigmoid, "linear": nn.Identity, "relu": nn.ReLU}
loss_fct_map = {
    "mse": nn.MSELoss(),
    "cross_entropy": nn.CrossEntropyLoss(),
    "bce": nn.BCELoss(),
}

model_type_map = {
    "mistral": ("model", MistralModel),
    "gpt2": ("transformer", GPT2Model),
    "llama": ("model", LlamaModel),
}