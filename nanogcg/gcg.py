import copy
import gc

from dataclasses import dataclass
from tqdm import tqdm
from typing import List, Optional, Union

import torch
import transformers
from torch import Tensor
from transformers import set_seed

from nanogcg.utils import INIT_CHARS, find_executable_batch_size, get_nonascii_toks, mellowmax

@dataclass
class GCGConfig:
    num_steps: int = 250
    optim_str_init: Union[str, List[str]] = "x x x x x x x x x x x x x x x x x x x x"
    search_width: int = 512
    batch_size: int = None
    topk: int = 256
    n_replace: int = 1
    buffer_size: int = 0
    use_mellowmax: bool = False
    mellowmax_alpha: float = 1.0
    allow_non_ascii: bool = False
    filter_ids: bool = True
    add_space_before_target: bool = False
    seed: int = None
    verbose: bool = False
    custom_loss_func: Optional[callable] = None
    loss_stopping_criteria: Optional[float] = -torch.inf # usually a loss value to stop the run if reached
    special_tokens_to_append: Optional[torch.Tensor] = None # special embeddings to be used in the special mode; must be a tensor of shape (n_special_tokens)
    custom_score_func: Optional[callable] = None

@dataclass
class GCGResult:
    best_loss: float
    best_score: float
    best_string: str
    losses: List[float]
    scores: List[float]
    strings: List[str]
    number_of_steps: int

class AttackBuffer:
    def __init__(self, size: int):
        self.buffer = [] # elements are (loss: float, optim_ids: Tensor)
        self.size = size

    def add(self, loss: float, optim_ids: Tensor) -> None:
        if self.size == 0:
            self.buffer = [(loss, optim_ids)]
            return

        if len(self.buffer) < self.size:
            self.buffer.append((loss, optim_ids))
            return

        self.buffer[-1] = (loss, optim_ids)
        self.buffer.sort(key=lambda x: x[0])

    def get_best_ids(self) -> Tensor:
        return self.buffer[0][1]

    def get_lowest_loss(self) -> float:
        return self.buffer[0][0]
    
    def get_highest_loss(self) -> float:
        return self.buffer[-1][0]
    
    def print_buffer(self, tokenizer):
        print("buffer:")
        for loss, ids in self.buffer:
            optim_str = tokenizer.batch_decode(ids)[0]
            optim_str = optim_str.replace("\\", "\\\\")
            optim_str = optim_str.replace("\n", "\\n")
            print(f"loss: {loss}" + f" | string: {optim_str}")
        print()

def sample_ids_from_grad(
    ids: Tensor, 
    grad: Tensor, 
    search_width: int, 
    topk: int = 256,
    n_replace: int = 1,
    not_allowed_ids: Tensor = False,
):
    """Returns `search_width` combinations of token ids based on the token gradient.

    Args:
        ids : Tensor, shape = (n_optim_ids)
            the sequence of token ids that are being optimized 
        grad : Tensor, shape = (n_optim_ids, vocab_size)
            the gradient of the GCG loss computed with respect to the one-hot token embeddings
        search_width : int
            the number of candidate sequences to return
        topk : int
            the topk to be used when sampling from the gradient
        n_replace: int
            the number of token positions to update per sequence
        not_allowed_ids: Tensor, shape = (n_ids)
            the token ids that should not be used in optimization
    
    Returns:
        sampled_ids : Tensor, shape = (search_width, n_optim_ids)
            sampled token ids
    """
    n_optim_tokens = len(ids)
    original_ids = ids.repeat(search_width, 1)

    if not_allowed_ids is not None:
        grad[:, not_allowed_ids.to(grad.device)] = float("inf")

    topk_ids = (-grad).topk(topk, dim=1).indices

    sampled_ids_pos = torch.argsort(torch.rand((search_width, n_optim_tokens), device=grad.device))[..., :n_replace]
    sampled_ids_val = torch.gather(
        topk_ids[sampled_ids_pos],
        2,
        torch.randint(0, topk, (search_width, n_replace, 1), device=grad.device)
    ).squeeze(2)

    new_ids = original_ids.scatter_(1, sampled_ids_pos, sampled_ids_val)

    return new_ids

def filter_ids(ids: Tensor, tokenizer: transformers.PreTrainedTokenizer):
    """Filters out sequeneces of token ids that change after retokenization.

    Args:
        ids : Tensor, shape = (search_width, n_optim_ids) 
            token ids 
        tokenizer : ~transformers.PreTrainedTokenizer
            the model's tokenizer
    
    Returns:
        filtered_ids : Tensor, shape = (new_search_width, n_optim_ids)
            all token ids that are the same after retokenization
    """
    ids_decoded = tokenizer.batch_decode(ids)
    filtered_ids = []

    for i in range(len(ids_decoded)):
        # Retokenize the decoded token ids
        ids_encoded = tokenizer(ids_decoded[i], return_tensors="pt", add_special_tokens=False).to(ids.device)["input_ids"][0]
        if torch.equal(ids[i], ids_encoded):
           filtered_ids.append(ids[i]) 
    
    if not filtered_ids:
        # This occurs in some cases, e.g. using the Llama-3 tokenizer with a bad initialization
        raise RuntimeError(
            "No token sequences are the same after decoding and re-encoding"
            "Consider setting `filter_ids=False` or trying a different `optim_str_init`"
        )
    
    return torch.stack(filtered_ids)

class GCG:
    def __init__(
        self, 
        model: transformers.PreTrainedModel,
        tokenizer: transformers.PreTrainedTokenizer,
        config: GCGConfig,
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.config = config

        self.embedding_layer = model.get_input_embeddings()
        self.not_allowed_ids = None if config.allow_non_ascii else get_nonascii_toks(tokenizer, device=model.device)
        
        # Next we assign the custom loss function if it is provided by the user
        # Note, that the custom loss function should take logits as input and return a loss tensor
        self.custom_loss_func = config.custom_loss_func
        self.custom_score_func = config.custom_score_func or (lambda x: x)  # Default to identity function if not provided
        self.stopping_point = config.loss_stopping_criteria
        self.stopping_flag = False # flag to stop the run if the loss is below a certain value
        
        
        self.special_mode_flag = True if config.special_tokens_to_append is not None else False # flag to indicate if the model is in a special mode
        if self.special_mode_flag:
            self.special_extra_embeds = self.embedding_layer(config.special_tokens_to_append)

        if model.dtype in (torch.float32, torch.float64):
            print(f"WARNING: Model is in {model.dtype}. Use a lower precision data type, if possible, for much faster optimization.")

        if model.device == torch.device("cpu"):
            print("WARNING: model is on the CPU. Use a hardware accelerator for faster optimization.")
    
    def run(
        self,
        messages: Union[str, List[dict]],
        target: str,
    ) -> GCGResult:
        model = self.model
        tokenizer = self.tokenizer
        config = self.config

        if config.seed is not None:
            set_seed(config.seed)
            torch.use_deterministic_algorithms(True, warn_only=True)
    
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        else:
            messages = copy.deepcopy(messages)
    
        # Append the GCG string at the end of the prompt if location not specified
        if not any(["{optim_str}" in d["content"] for d in messages]):
            messages[-1]["content"] = messages[-1]["content"] + "{optim_str}"

        template = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True) 
        # Remove the BOS token -- this will get added when tokenizing, if necessary
        if tokenizer.bos_token and template.startswith(tokenizer.bos_token):
            template = template.replace(tokenizer.bos_token, "")
        before_str, after_str = template.split("{optim_str}")

        target = " " + target if config.add_space_before_target else target

        # Tokenize everything that doesn't get optimized
        before_ids = tokenizer([before_str], padding=False, return_tensors="pt")["input_ids"].to(model.device)
        after_ids = tokenizer([after_str], add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
        target_ids = tokenizer([target], add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)

        # Embed everything that doesn't get optimized
        embedding_layer = self.embedding_layer
        before_embeds, after_embeds, target_embeds = [embedding_layer(ids) for ids in (before_ids, after_ids, target_ids)]

        # Compute the KV Cache for tokens that appear before the optimized tokens
        with torch.no_grad():
            output = model(inputs_embeds=before_embeds, use_cache=True)
            self.prefix_cache = output.past_key_values
        
        self.target_ids = target_ids
        self.after_embeds = after_embeds
        self.target_embeds = target_embeds

        # Initialize the attack buffer
        buffer = self.init_buffer()
        optim_ids = buffer.get_best_ids()

        losses = []
        scores = []
        optim_strings = []
        
        for i in tqdm(range(config.num_steps)):

            # Compute the token gradient
            optim_ids_onehot_grad = self.compute_token_gradient(optim_ids, target_ids) 

            with torch.no_grad():

                # Sample candidate token sequences based on the token gradient
                sampled_ids = sample_ids_from_grad(
                    optim_ids.squeeze(0),
                    optim_ids_onehot_grad.squeeze(0),
                    config.search_width,
                    config.topk,
                    config.n_replace,
                    not_allowed_ids=self.not_allowed_ids,
                )

                if config.filter_ids:
                    sampled_ids = filter_ids(sampled_ids, tokenizer)

                new_search_width = sampled_ids.shape[0]

                # Compute loss on all candidate sequences 
                batch_size = new_search_width if config.batch_size is None else config.batch_size
                if not self.special_mode_flag:
                    input_embeds = torch.cat([
                        embedding_layer(sampled_ids),
                        after_embeds.repeat(new_search_width, 1, 1),
                        target_embeds.repeat(new_search_width, 1, 1)
                    ], dim=1)
                else:
                    input_embeds = torch.cat([
                        embedding_layer(sampled_ids),
                        after_embeds.repeat(new_search_width, 1, 1),
                        self.special_extra_embeds.repeat(new_search_width, 1, 1)
                    ], dim=1)
                loss = find_executable_batch_size(self.compute_candidates_loss, batch_size)(
                    input_embeds,
                    target_ids
                )

                current_loss = loss.min().item()
                current_score = self.custom_score_func(current_loss)
                optim_ids = sampled_ids[loss.argmin()].unsqueeze(0)

                # Update the buffer based on the loss
                losses.append(current_loss)
                scores.append(current_score)
                if buffer.size == 0 or current_loss < buffer.get_highest_loss():
                    buffer.add(current_loss, optim_ids)
                    
            self.stopping_flag = current_loss < self.stopping_point     

            optim_ids = buffer.get_best_ids()
            optim_str = tokenizer.batch_decode(optim_ids)[0]
            optim_strings.append(optim_str)
            
            if config.verbose:
                print(f"\noptim_str: {optim_str}\nloss: {current_loss}\nProb score: {current_score}")

            # TODO: set up buffer properly to handle scores, and understand it
            # if not config.verbose:
            #     print(f"step: {i+1}\noptim_str: {optim_str}\nloss: {current_loss}\nscore: {current_score}")
            # else:
            #     print(f"step: {i+1}")
            #     buffer.print_buffer(tokenizer)
            
            if self.stopping_flag:
                print(f"Stopping at iteration {i} to loss stopping criteria.")
                break

        min_loss_index = losses.index(min(losses)) 

        result = GCGResult(
            best_loss=losses[min_loss_index],
            best_score=scores[min_loss_index],
            best_string=optim_strings[min_loss_index],
            losses=losses,
            scores=scores,
            strings=optim_strings,
            number_of_steps=i+1
        )

        return result
    
    def init_buffer(self) -> AttackBuffer:
        model = self.model
        tokenizer = self.tokenizer
        config = self.config

        if config.verbose:
            print(f"Initializing attack buffer of size {config.buffer_size}...")

        # Create the attack buffer and initialize the buffer ids
        buffer = AttackBuffer(config.buffer_size)

        if isinstance(config.optim_str_init, str):
            init_optim_ids = tokenizer(config.optim_str_init, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
            if config.buffer_size > 1:
                init_buffer_ids = tokenizer(INIT_CHARS, add_special_tokens=False, return_tensors="pt")["input_ids"].squeeze().to(model.device, dtype=torch.float32)
                init_buffer_ids = [init_buffer_ids[torch.multinomial(init_buffer_ids, init_optim_ids.shape[1], replacement=True)].unsqueeze(0).long() for _ in range(config.buffer_size - 1)]
                init_buffer_ids = torch.cat([init_optim_ids] + init_buffer_ids, dim=0)
            else:
                init_buffer_ids = init_optim_ids
                
        else: # assume list
            if (len(config.optim_str_init) != config.buffer_size):
                print(f"WARNING: Using {len(config.optim_str_init)} initializations but buffer size is set to {config.buffer_size}")
            try:
                init_buffer_ids = tokenizer(config.optim_str_init, add_special_tokens=False, return_tensors="pt")["input_ids"].to(model.device)
            except ValueError:
                print("Unable to create buffer. Ensure that all initializations tokenize to the same length.")

        true_buffer_size = max(1, config.buffer_size) 

        # Compute the loss on the initial buffer entries
        init_buffer_embeds = torch.cat([
            self.embedding_layer(init_buffer_ids),
            self.after_embeds.repeat(true_buffer_size, 1, 1),
            self.target_embeds.repeat(true_buffer_size, 1, 1),
        ], dim=1)
        init_buffer_losses = find_executable_batch_size(self.compute_candidates_loss, true_buffer_size)(
            init_buffer_embeds,
            self.target_ids,
        )

        # Populate the buffer
        for i in range(true_buffer_size):
            buffer.add(init_buffer_losses[i], init_buffer_ids[[i]])

        if config.verbose:
            print("Initialized attack buffer.")
        
        return buffer
    
    def compute_token_gradient(
        self,
        optim_ids: Tensor,
        target_ids: Tensor,
    ) -> Tensor:
        """Computes the gradient of the GCG loss w.r.t the one-hot token matrix.

        Args:
        optim_ids : Tensor, shape = (1, n_optim_ids)
            the sequence of token ids that are being optimized 
        target_ids : Tensor, shape = (1, n_target_ids)
            the token ids of the target sequence
        """
        model = self.model
        embedding_layer = self.embedding_layer

        # Create the one-hot encoding matrix of our optimized token ids
        optim_ids_onehot = torch.nn.functional.one_hot(optim_ids, num_classes=embedding_layer.num_embeddings)
        optim_ids_onehot = optim_ids_onehot.to(dtype=model.dtype, device=model.device)
        optim_ids_onehot.requires_grad_()

        # (1, num_optim_tokens, vocab_size) @ (vocab_size, embed_dim) -> (1, num_optim_tokens, embed_dim)
        optim_embeds = optim_ids_onehot @ embedding_layer.weight

        if not self.special_mode_flag:
            input_embeds = torch.cat([optim_embeds, self.after_embeds, self.target_embeds], dim=1)
        else:
            input_embeds = torch.cat([optim_embeds, self.after_embeds, self.special_extra_embeds], dim=1)
        output = model(inputs_embeds=input_embeds, past_key_values=self.prefix_cache)
        logits = output.logits

        if self.custom_loss_func is not None:
            loss = self.custom_loss_func(logits)
        else:
            # Shift logits so token n-1 predicts token n
            shift = input_embeds.shape[1] - target_ids.shape[1]
            shift_logits = logits[..., shift-1:-1, :].contiguous() # (1, num_target_ids, vocab_size)
            shift_labels = target_ids
            if self.config.use_mellowmax:
                label_logits = torch.gather(shift_logits, -1, shift_labels.unsqueeze(-1)).squeeze(-1)
                loss = mellowmax(-label_logits, alpha=self.config.mellowmax_alpha, dim=-1)
            else:
                loss = torch.nn.functional.cross_entropy(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        optim_ids_onehot_grad = torch.autograd.grad(outputs=[loss], inputs=[optim_ids_onehot])[0]

        return optim_ids_onehot_grad
    
    def compute_candidates_loss(
        self,
        search_batch_size: int, 
        input_embeds: Tensor, 
        target_ids: Tensor,
    ) -> Tensor:
        """Computes the GCG loss on all candidate token id sequences.

        Args:
            search_batch_size : int
                the number of candidate sequences to evaluate in a given batch
            input_embeds : Tensor, shape = (search_width, seq_len, embd_dim)
                the embeddings of the `search_width` candidate sequences to evaluate
            target_ids : Tensor, shape = (1, n_target_ids)
                the token ids of the target sequence 
        """
        all_loss = []
        prefix_cache_batch = []
        prefix_cache = self.prefix_cache
        for i in range(0, input_embeds.shape[0], search_batch_size):
            with torch.no_grad():
                input_embeds_batch = input_embeds[i:i+search_batch_size]
                current_batch_size = input_embeds_batch.shape[0]

                if not prefix_cache_batch or current_batch_size != search_batch_size:
                    prefix_cache_batch = [[x.expand(current_batch_size, -1, -1, -1) for x in prefix_cache[i]] for i in range(len(prefix_cache))]

                outputs = self.model(inputs_embeds=input_embeds_batch, past_key_values=prefix_cache_batch)
                logits = outputs.logits

                if self.custom_loss_func is not None:
                    loss = self.custom_loss_func(logits)
                else:
                    tmp = input_embeds.shape[1] - target_ids.shape[1]
                    shift_logits = logits[..., tmp-1:-1, :].contiguous()
                    shift_labels = target_ids.repeat(current_batch_size, 1)
                    
                    if self.config.use_mellowmax:
                        label_logits = torch.gather(shift_logits, -1, shift_labels.unsqueeze(-1)).squeeze(-1)
                        loss = mellowmax(-label_logits, alpha=self.config.mellowmax_alpha, dim=-1)
                    else:
                        loss = torch.nn.functional.cross_entropy(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1), reduction="none")

                loss = loss.view(current_batch_size, -1).mean(dim=-1)
                all_loss.append(loss)

                del outputs
                gc.collect()
                torch.cuda.empty_cache()

        return torch.cat(all_loss, dim=0)

# A wrapper around the GCG `run` method that provides a simple API
def run(
    model: transformers.PreTrainedModel,
    tokenizer: transformers.PreTrainedTokenizer,
    messages: Union[str, List[dict]],
    target: str,
    config: Optional[GCGConfig] = None, 
) -> GCGResult:
    """Generates a single optimized string using GCG. 

    Args:
        model: The model to use for optimization.
        tokenizer: The model's tokenizer.
        messages: The conversation to use for optimization.
        target: The target generation.
        config: The GCG configuration to use.
    
    Returns:
        A GCGResult object that contains losses and the optimized strings.
    """
    if config is None:
        config = GCGConfig()
    
    gcg = GCG(model, tokenizer, config)
    result = gcg.run(messages, target)
    return result
    
