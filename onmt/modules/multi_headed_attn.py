""" Multi-Head Attention module """
import math
import torch
from torch import Tensor
from typing import Optional, Tuple
import torch.nn as nn
from torch.nn.utils import skip_init
from .alibi_position_bias import AlibiPositionalBias


# Help functions for Rotary Embeddings
# https://arxiv.org/pdf/2104.09864.pdf
# too convoluted to make maxseqlen a parameter.
# we suppose src_seq_len at training and max_length at inference
# are both < 2048 tokens.


def rotaryembeddings(dim: int, maxseqlen=4096, base=10000):
    inv_freq = 1.0 / (base ** (torch.arange(0, dim, 2).float() / dim))
    tmax = torch.arange(maxseqlen, device=inv_freq.device)
    rope = torch.outer(tmax, inv_freq).float()
    # rope is now matrix [maxseqlen, dim/2]
    rope = torch.polar(torch.ones_like(rope), rope)
    return rope


def apply_rotary_emb(query, key, rope):
    query_ = query.float().reshape(*query.shape[:-1], -1, 2)
    query_ = torch.view_as_complex(query_)
    key_ = key.float().reshape(*key.shape[:-1], -1, 2)
    key_ = torch.view_as_complex(key_)
    rope = rope.view(1, query_.size(1), 1, query_.size(3))
    query_out = torch.view_as_real(query_ * rope).flatten(3)
    key_out = torch.view_as_real(key_ * rope).flatten(3)
    return query_out.type_as(query), key_out.type_as(key)


# Help functions for max_relative positions
# https://arxiv.org/abs/1803.02155


def relative_matmul(x: Tensor, z: Tensor, transpose: bool) -> Tensor:
    """
    Helper function for relative positions attention.
    https://arxiv.org/pdf/1803.02155.pdf
    x shape [batch_size x heads x q_len x k_len]
    """
    batch_size = x.size(0)
    heads = x.size(1)
    length = x.size(2)
    x_t = x.permute(2, 0, 1, 3)
    x_t_r = x_t.contiguous().view(length, heads * batch_size, -1)
    if transpose:
        z = z.transpose(1, 2)
    x_tz_matmul = torch.matmul(x_t_r, z)
    x_tz_matmul_r = x_tz_matmul.view(length, batch_size, heads, -1)
    x_tz_matmul_r_t = x_tz_matmul_r.permute(1, 2, 0, 3)
    return x_tz_matmul_r_t


def gen_relative_positions(
    length: int,
    max_relative_positions: int,
    cache: bool = False,
    device: Optional[torch.device] = None,
) -> Tensor:
    """Generate the clipped relative positions matrix
    for a given length and maximum relative positions"""
    if cache:
        distance_mat = torch.arange(-length + 1, 1, 1, device=device).unsqueeze(0)
    else:
        range_vec = torch.arange(length, device=device)
        range_mat = range_vec.unsqueeze(-1).expand(-1, length).transpose(0, 1)
        distance_mat = range_mat - range_mat.transpose(0, 1)
    distance_mat_clipped = torch.clamp(
        distance_mat, min=-max_relative_positions, max=max_relative_positions
    )
    # Shift values to be >= 0
    final_mat = distance_mat_clipped + max_relative_positions
    return final_mat


# Help functions to split model dim per head


def shape(x: Tensor, dim_per_head: int) -> Tensor:
    """
    Projection.
    [batchsize x length x modeldim]
    -> [batchsize x heads x length x dimperhead]
    """
    return x.view(x.size(0), x.size(1), -1, dim_per_head).transpose(1, 2)


def unshape(x: Tensor) -> Tensor:
    """
    Compute context.
    [batchsize x heads x length x dimperhead]
    -> [batchsize x length x modeldim]
    """
    return x.transpose(1, 2).contiguous().view(x.size(0), -1, x.size(1) * x.size(3))


class MultiHeadedAttention(nn.Module):
    # class MultiHeadedAttention(torch.jit.ScriptModule):
    """Multi-Head Attention module from "Attention is All You Need"
    :cite:`DBLP:journals/corr/VaswaniSPUJGKP17`.

    Similar to standard `dot` attention but uses
    multiple attention distributions simulataneously
    to select relevant items.

    .. mermaid::

       graph BT
          A[key]
          B[value]
          C[query]
          O[output]
          subgraph Attn
            D[Attn 1]
            E[Attn 2]
            F[Attn N]
          end
          A --> D
          C --> D
          A --> E
          C --> E
          A --> F
          C --> F
          D --> O
          E --> O
          F --> O
          B --> O

    Also includes several additional tricks.

    Args:
       head_count (int): number of parallel heads
       model_dim (int): the dimension of keys/values/queries,
           must be divisible by head_count
       dropout (float): dropout parameter
       max_relative_positions (int): max relative positions
       attn_type: "self" or "context"
    """

    def __init__(
        self,
        head_count: int,
        model_dim: int,
        dropout: float = 0.1,
        max_relative_positions: int = 0,
        attn_type: str = None,
        add_qkvbias=False,
        num_kv=0,
        use_ckpting=[],
    ) -> None:
        assert model_dim % head_count == 0
        self.dim_per_head = model_dim // head_count
        super(MultiHeadedAttention, self).__init__()
        self.head_count = head_count
        self.linear_keys = skip_init(
            nn.Linear, in_features=model_dim, out_features=model_dim, bias=add_qkvbias
        )
        self.linear_values = skip_init(
            nn.Linear, in_features=model_dim, out_features=model_dim, bias=add_qkvbias
        )
        self.linear_query = skip_init(
            nn.Linear, in_features=model_dim, out_features=model_dim, bias=add_qkvbias
        )
        self.softmax = nn.Softmax(dim=-1)
        self.dropout = nn.Dropout(dropout)
        self.final_linear = skip_init(
            nn.Linear, in_features=model_dim, out_features=model_dim, bias=add_qkvbias
        )

        self.max_relative_positions = max_relative_positions
        self.attn_type = attn_type
        self.layer_cache = (
            False,
            {"keys": torch.tensor([]), "values": torch.tensor([])},
        )
        if max_relative_positions > 0:
            # https://arxiv.org/pdf/1803.02155.pdf
            # in the paper they suggest either two embeds
            # relative_key / relative_value or only
            # relative_key. We implemented the same embed
            # for both.
            vocab_size = max_relative_positions * 2 + 1
            self.relative_positions_embeddings = nn.Embedding(
                vocab_size, self.dim_per_head
            )
        else:
            self.relative_positions_embeddings = None

            if max_relative_positions == -1:  # rotary embeddings
                self.rope = rotaryembeddings(self.dim_per_head)

            if max_relative_positions == -2:  # alibi positional bias
                self.alibi = AlibiPositionalBias(head_count)

        self.maybe_ckpt = checkpoint if "mha" in use_ckpting else lambda f, x: f(x)

    def update_dropout(self, dropout: float) -> None:
        self.dropout.p = dropout

    # @torch.jit.script_method
    def forward(
        self,
        key: Tensor,
        value: Tensor,
        query: Tensor,
        mask: Optional[Tensor] = None,
        step: Optional[int] = 0,
    ) -> Tuple[Tensor, Tensor]:
        """
        Compute the context vector and the attention vectors.

        Args:
           key (Tensor): set of `key_len`
               key vectors ``(batch, key_len, dim)``
           value (Tensor): set of `key_len`
               value vectors ``(batch, key_len, dim)``
           query (Tensor): set of `query_len`
               query vectors  ``(batch, query_len, dim)``
           mask: binary mask 1/0 indicating which keys have
               zero / non-zero attention ``(batch, query_len, key_len)``
           step (int): decoding step (used for Rotary embedding)
        Returns:
           (Tensor, Tensor):

           * output context vectors ``(batch, query_len, dim)``
           * Attention vector in heads ``(batch, head, query_len, key_len)``.
        """
        # 1) Project key, value, and query.
        # as a reminder at training layer_cache[0] remains False
        if self.layer_cache[0]:
            if self.attn_type == "self":
                query, key, value = (
                    self.linear_query(query),
                    self.linear_keys(query),
                    self.linear_values(query),
                )
                query = shape(query, self.dim_per_head)
                key = shape(key, self.dim_per_head)
                value = shape(value, self.dim_per_head)

                if self.max_relative_positions == -1:  # Rotary Embeddings
                    start_pos = step
                    seqlen = query.size(2)
                    rope = self.rope[start_pos : start_pos + seqlen].to(query.device)

                    query = query.transpose(1, 2)
                    key = key.transpose(1, 2)
                    query, key = apply_rotary_emb(query, key, rope=rope)
                    query = query.transpose(1, 2)
                    key = key.transpose(1, 2)

                if self.layer_cache[1]["keys"].numel() != 0:
                    key = torch.cat((self.layer_cache[1]["keys"], key), dim=2)

                if self.layer_cache[1]["values"].numel() != 0:
                    value = torch.cat((self.layer_cache[1]["values"], value), dim=2)
                self.layer_cache[1]["keys"] = key
                self.layer_cache[1]["values"] = value
            elif self.attn_type == "context":
                query = self.linear_query(query)
                query = shape(query, self.dim_per_head)
                if self.layer_cache[1]["keys"].numel() == 0:
                    key, value = self.linear_keys(key), self.linear_values(value)
                    key = shape(key, self.dim_per_head)
                    value = shape(value, self.dim_per_head)
                else:
                    key, value = (
                        self.layer_cache[1]["keys"],
                        self.layer_cache[1]["values"],
                    )
                self.layer_cache[1]["keys"] = key
                self.layer_cache[1]["values"] = value
        else:
            key = self.maybe_ckpt(self.linear_keys, key)
            value = self.maybe_ckpt(self.linear_values, value)
            query = self.maybe_ckpt(self.linear_query, query)
            key = shape(key, self.dim_per_head)
            value = shape(value, self.dim_per_head)
            query = shape(query, self.dim_per_head)

            if self.max_relative_positions == -1:  # Rotary Embeddings
                start_pos = 0
                seqlen = query.size(2)
                rope = self.rope[start_pos : start_pos + seqlen].to(query.device)

                query = query.transpose(1, 2)
                key = key.transpose(1, 2)
                query, key = apply_rotary_emb(query, key, rope=rope)
                query = query.transpose(1, 2)
                key = key.transpose(1, 2)
        # 2) Calculate and scale scores.
        query /= math.sqrt(self.dim_per_head)
        # expand key on heads dimension when it's less than query heads (multi-query variant)
        key = key.view(key.size(0), -1, 1, key.size(2), key.size(3)).repeat(
            1, 1, query.size(1) // key.size(1), 1, 1
        )
        key = key.view(key.size(0), query.size(1), key.size(3), key.size(4))
        # batch x num_heads x query_len x key_len
        scores = torch.matmul(query, key.transpose(2, 3))

        if self.relative_positions_embeddings is not None:
            key_len = key.size(2)
            # 1 or key_len x key_len
            relative_positions_matrix = gen_relative_positions(
                key_len,
                self.max_relative_positions,
                cache=self.layer_cache[0],
                device=key.device,
            )
            #  1 or key_len x key_len x dim_per_head
            relations_keys = self.relative_positions_embeddings(
                relative_positions_matrix
            )
            scores.add_(relative_matmul(query, relations_keys, True))
        elif self.max_relative_positions == -2:  # Alibi
            scores = self.alibi(scores)

        scores = scores.float()

        if mask is not None:
            # not 100% necessary but expand to nb of heads
            mask = mask.expand(-1, self.head_count, -1, -1)
            # now mask and scores have the same shape
            scores = scores.masked_fill(mask, -1e18)

        # 3) Apply attention dropout and compute context vectors.
        attn = self.softmax(scores).to(query.dtype)
        drop_attn = self.dropout(attn)
        # expand value on heads dimension when it's less than query heads (multi-query variant)
        value = value.view(value.size(0), -1, 1, value.size(2), value.size(3)).repeat(
            1, 1, query.size(1) // value.size(1), 1, 1
        )
        value = value.view(value.size(0), query.size(1), value.size(3), value.size(4))
        context_original = torch.matmul(drop_attn, value)

        if self.relative_positions_embeddings is not None:
            # We use the same embeddings for key and value
            relations_values = relations_keys
            context_original.add_(relative_matmul(drop_attn, relations_values, False))

        context = unshape(context_original)

        attn_output = self.maybe_ckpt(self.final_linear, context)

        return attn_output, attn
