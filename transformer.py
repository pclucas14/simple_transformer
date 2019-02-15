import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import math, copy, time
from torch.autograd import Variable
from torch.distributions import Categorical
import matplotlib.pyplot as plt
import seaborn

'''
Code taken from http://nlp.seas.harvard.edu/2018/04/03/attention.html
'''

class Transformer(nn.Module):
    """
    Actually Transformer Decoder if you wanna be on point
    """
    def __init__(self, args): 
        super(Transformer, self).__init__()
        
        # create ind. parts
        c = copy.deepcopy
        attn = MultiHeadedAttention(args.n_heads, args.d_model, max_seq_len=args.max_seq_len)
        ff = PositionwiseFeedForward(args.d_model, args.d_ff, args.dropout)
        position = PositionalEncoding(args.d_model, args.dropout)
        
        # create layers
        layer = DecoderLayer(args.d_model, attn, ff, args.dropout)
        embed = nn.Sequential(Embeddings(args.d_model, args.vocab_size), c(position))
        gen_w = Generator(args.d_model, args.vocab_size) 

        self.decoder   = Decoder(layer, args.n_layers)
        self.inp_embed = embed
        self.gen_word  = gen_w
        self.pos_embed = c(position)
        self.args      = args

        if args.mode == 'PTW':
            self.gen_pos = Generator(args.d_model, args.max_seq_len + 1)
        
    def forward(self, inp, inp_mask, target_pos=None, tf_pos=None):
        "Take in and process masked src and target sequences."
        # TODO: we need to make this 2 step if mode == PTW. 
        # 1st decode for positional dist. (easy, just use another generator layer with correct dim.)
        # 2nd condition on the (teacher forced) position. This is less obvious; possible ways are to 
        # convolve gaussian filter on the correct position --> conceptually good, but might be hard to implement eff.
        # skip connection with fixed positional embeddings. maybe try fancier combinations like gated_linear.
        hidden_rep = self.decode(inp, inp_mask, target_pos, tf_pos)
        if self.args.mode == 'PTW':
            pos_logits = self.gen_pos(hidden_rep, return_logits=True)
            
            # now we condition hidden rep on the position for which it needs to predict words
            trg_pos_embeds = self.pos_embed(hidden_rep, pos_only=True, trg_pos=tf_pos)
            hidden_rep = hidden_rep + trg_pos_embeds
            word_logits = self.gen_word(hidden_rep, return_logits=True)

            return word_logits, pos_logits

        else: 
            return self.gen_word(hidden_rep)

        return self.generator(self.decode(inp, inp_mask, target_pos, tf_pos))

    def decode(self, inp, inp_mask, target_pos, tf_pos=None):
        # this next line is for the VAE setup, where input could be latent z's
        inp = self.inp_embed(inp) if 'Long' in str(inp.type()) else inp

        # this is for randomized ordering --> we need to tell the network what position it needs to predict
        if target_pos is not None:
            if 'Long' in str(target_pos.type()):
                target_pos = self.pos_embed(inp, pos_only=True, trg_pos=target_pos)

        return self.decoder(inp, inp_mask, target_pos)


class Generator(nn.Module):
    """ Define standard linear + softmax generation step """
    def __init__(self, d_model, vocab):
        super(Generator, self).__init__()
        self.proj = nn.Linear(d_model, vocab)

    def forward(self, x, return_logits=False):
        if return_logits:
            return self.proj(x)
        else:
            return F.log_softmax(self.proj(x), dim=-1)


def clones(module, N):
    """ Produce N identical layers """
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


class LayerNorm(nn.Module):
    """ Construct a layernorm module (See citation for details) """
    def __init__(self, features, eps=1e-6):
        super(LayerNorm, self).__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2


class SublayerConnection(nn.Module):
    """
    A residual connection followed by a layer norm.
    Note for code simplicity the norm is first as opposed to last.
    """
    def __init__(self, size, dropout):
        super(SublayerConnection, self).__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        "Apply residual connection to any sublayer with the same size."
        return x + self.dropout(sublayer(self.norm(x)))


class Decoder(nn.Module):
    """ Generic N layer decoder with masking """
    def __init__(self, layer, N):
        super(Decoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)
        
    def forward(self, x, inp_mask, target_pos_embeddings):
        for layer in self.layers:
            x = layer(x, inp_mask, target_pos_embeddings=target_pos_embeddings)
        return self.norm(x)


class DecoderLayer(nn.Module):
    """ Decoder is made of self-attn, src-attn, and feed forward (defined below) """
    def __init__(self, size, self_attn, feed_forward, dropout):
        super(DecoderLayer, self).__init__()
        self.size = size
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 2) 
 
    def forward(self, x, inp_mask, target_pos_embeddings=None):
        "Follow Figure 1 (right) for connections."
        x = self.sublayer[0](x, lambda x: \
                    self.self_attn(x, x, x, inp_mask, trg_pos_embed=target_pos_embeddings))
        if target_pos_embeddings is not None:
            x += target_pos_embeddings

        return self.sublayer[1](x, self.feed_forward)


def subsequent_mask(size):
    """ Mask out subsequent positions. """
    attn_shape = (1, size, size)
    subsequent_mask = np.triu(np.ones(attn_shape), k=1).astype('uint8')
    return torch.from_numpy(subsequent_mask) == 0


def attention(query, key, value, mask=None, dropout=None, pos_embeds=None):
    """ Compute 'Scaled Dot Product Attention' """
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1)) \
             / math.sqrt(d_k)

    if pos_embeds is not None:
        bs, n_heads, seq_len, dim = key.size()
        cumsum = torch.cumsum(mask, dim=-1)
        # note that cumsum indices also have "useless indices", which get masked away
        # eg [1, 0, 0, 1] --> [1, 1, 1, 2], but we only care about [1, -, -, 2]
        
        # next, we need to do -1 since we want actual indices
        cumsum = (cumsum - 1)# .clamp_(min=0)

        # to make debugging easier, let's actually mask out unused indices
        # note that we could also clamp to remove -1's. 
        
        # we multiply by the sequence length (the offset from indexing 2D arr. in 1D)
        non_zero = (mask != 0).long()
        cumsum = cumsum * non_zero * seq_len
    
        offset = torch.arange(seq_len).view(1, -1).expand(seq_len, -1).long()
        if key.is_cuda: offset = offset.cuda()

        indices = (offset + cumsum) * non_zero

        # since `indices` come from `mask`, they don't have the correct amt of n_heads
        indices = indices.expand(-1, n_heads, -1, -1)

        # all that remains index wise is to account for the bs and h_heads dimension
        indices = indices.reshape(bs * n_heads, seq_len ** 2)
        offset = torch.arange(bs * n_heads).view(-1, 1).long()  * seq_len * seq_len
        if key.is_cuda: offset = offset.cuda()
        
        indices = indices + offset

        # NOW indices are ready to take values
        pos_embeds = pos_embeds[:, :seq_len] # + 1] # +1  for debugging only

        rel_scores = torch.einsum('bhld,hmd->bhml', (query, pos_embeds)) / math.sqrt(d_k)
        rel_scores_ = torch.take(rel_scores, indices).view(*scores.size())

        # masking out here also for easier debugging
        rel_scores_ *= non_zero.float()

        scores = scores + rel_scores_

    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    p_attn = F.softmax(scores, dim = -1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn


class MultiHeadedAttention(nn.Module):
    def __init__(self, h, d_model, dropout=0.1, use_relative_embeddings=True, max_seq_len=None):
        "Take in model size and number of heads."
        super(MultiHeadedAttention, self).__init__()
        assert d_model % h == 0
        # We assume d_v always equals d_k
        self.d_k = d_model // h
        self.h = h
        self.linears = clones(nn.Linear(d_model, d_model), 3) #4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

        self.rel_embed = None
        if use_relative_embeddings: 
            # create the embeddings
            embeds = torch.FloatTensor(h, max_seq_len + 1, self.d_k).normal_()
            self.rel_embed = nn.Parameter(embeds)
           

    def forward(self, query, key, value, mask=None, trg_pos_embed=None):
        "Implements Figure 2"
        if mask is not None:
            # Same mask applied to all h heads.
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)
        
        # 1) Do all the linear projections in batch from d_model => h x d_k
        query, key, value = \
            [l(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2)
             for l, x in zip(self.linears, (query, key, value))]

        # 2) Apply attention on all the projected vectors in batch. 
        x, self.attn = attention(query, key, value, mask=mask, 
                                 dropout=self.dropout, pos_embeds=self.rel_embed)
        
        # 3) "Concat" using a view and apply a final linear. 
        x = x.transpose(1, 2).contiguous() \
             .view(nbatches, -1, self.h * self.d_k)
        return self.linears[-1](x)


class PositionwiseFeedForward(nn.Module):
    "Implements FFN equation."
    def __init__(self, d_model, d_ff, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(F.relu(self.w_1(x))))


class Embeddings(nn.Module):
    def __init__(self, d_model, vocab):
        super(Embeddings, self).__init__()
        self.lut = nn.Embedding(vocab, d_model)
        self.d_model = d_model

    def forward(self, x):
        return self.lut(x) * math.sqrt(self.d_model)


class PositionalEncoding(nn.Module):
    "Implement the PE function."
    def __init__(self, d_model, dropout, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)
        
        # Compute the positional encodings once in log space.
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1).float()
        div_term = torch.exp(torch.arange(0, d_model, 2).float() *
                             -(math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)
        
    def forward(self, x, pos_only=False, trg_pos=None):
        pos = Variable(self.pe[:, :x.size(1)], requires_grad=False)
        if not pos_only:
            x = x + pos
        else:
            assert trg_pos is not None
            trg_pos_embed = torch.index_select(pos, 1, trg_pos.flatten())
            trg_pos_embed = trg_pos_embed.view(trg_pos.size(0), trg_pos.size(1), pos.size(-1))
            x = trg_pos_embed
        return self.dropout(x)


def make_vae(tgt_vocab, N=6, z_dim=64, d_model=512, d_ff=2048, h=8, dropout=0.1):
    "Helper: Construct a model from hyperparameters."
    c = copy.deepcopy
    attn = MultiHeadedAttention(h, d_model)
    ff = PositionwiseFeedForward(d_model, d_ff, dropout)
    position = PositionalEncoding(d_model, dropout)
    
    class VAE(nn.Module):
        def __init__(self):
            super(VAE, self).__init__()
            
            self.enc = Model(
                Decoder(DecoderLayer(d_model, c(attn), c(ff), dropout), N),
                nn.Sequential(Embeddings(d_model, tgt_vocab), c(position)),
                nn.Linear(d_model, z_dim), None)

            self.dec = Model(
                Decoder(DecoderLayer(d_model, c(attn), c(ff), dropout), N),
                nn.Sequential(Embeddings(d_model, tgt_vocab), c(position)),
                Generator(d_model, tgt_vocab), c(position))
            
            self.z_to_h = nn.Linear(z_dim, d_model)

            # tie weights
            self.enc.inp_embed.weight = self.dec.generator.proj.weight

        def forward(self, x, tgt_mask, target_pos=None):
            # source mask only masks the padding 
            src_mask = x.unsqueeze(-1).expand_as(tgt_mask) != 1  # pad tokens
            src_mask = (src_mask + src_mask.transpose(2,1)) == 2 # make it square
            z = self.enc(x, src_mask)
            # we sum out the sequence axis to get a single z vector
            z = z.sum(dim=1)

            # reparam trick
            # TODO

            h = self.z_to_h(z)

            # we will add h as a token in each sentence, and let the model cond. on h
            h = h.unsqueeze(1)
            x = self.enc.inp_embed(x)
            x = torch.cat([h, x], dim=1)
            tgt_mask = torch.cat([torch.ones_like(tgt_mask[:, :, [0]]),  tgt_mask], dim=2)
            tgt_mask = torch.cat([torch.zeros_like(tgt_mask[:, [0], :]), tgt_mask], dim=1)
         
            if target_pos is not None:
                target_pos = self.dec.pos_embed(x, pos_only=True, trg_pos=target_pos)
                target_pos = torch.cat([torch.zeros_like(target_pos[:, [0], :]), target_pos], dim=1)
         
            out = self.dec(x, tgt_mask, target_pos)
            return out[:, 1:, :].contiguous()
            
    return VAE()     

def make_model(tgt_vocab, N=6, 
               d_model=512, d_ff=2048, h=8, dropout=0.1):
    "Helper: Construct a model from hyperparameters."
    c = copy.deepcopy
    attn = MultiHeadedAttention(h, d_model)
    ff = PositionwiseFeedForward(d_model, d_ff, dropout)
    position = PositionalEncoding(d_model, dropout)
    model = Model(
        Decoder(DecoderLayer(d_model, c(attn), 
                             c(ff), dropout), N),
        nn.Sequential(Embeddings(d_model, tgt_vocab), c(position)),
        Generator(d_model, tgt_vocab), c(position))
    
    # This was important from their code. 
    # Initialize parameters with Glorot / fan_avg.
    for p in model.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)
    return model


def make_std_mask(tgt, pad):
    "Create a mask to hide padding and future words."
    tgt_mask = (tgt != pad).unsqueeze(-2)
    tgt_mask = tgt_mask & Variable(
        subsequent_mask(tgt.size(-1)).type_as(tgt_mask.data))
    return tgt_mask


# Layers
# ------------------------------------------------------------------------------------
''' Neat way of doing  ResNet while changing the dimension of the representation'''
class GatedDense(nn.Module):
    def __init__(self, input_size, output_size, activation=None):
        super(GatedDense, self).__init__()

        self.activation = activation
        self.sigmoid = nn.Sigmoid()
        self.h = nn.Linear(input_size, output_size)
        self.g = nn.Linear(input_size, output_size)

    def forward(self, x):
        h = self.h(x)
        if self.activation is not None:
            h = self.activation(h)

        g = self.sigmoid(self.g(x))

        return h * g



# Sampling
# ------------------------------------------------------------------------------------

def greedy_decode(model, max_len, start_symbol=2, take_max=False):
    ys = torch.ones(1, 1).fill_(start_symbol).long().cuda()
    for i in range(max_len-1):
        out = model.decode(
                           ys,
                           (subsequent_mask(ys.size(1)).type_as(ys)), target_pos=None)
        prob = model.generator(out[:, -1])
        if take_max:
            _, next_word = torch.max(prob, dim = 1)
        else:
            dist = torch.distributions.Categorical(prob.exp())
            next_word = dist.sample()
        next_word = next_word.data[0]
        ys = torch.cat([ys,
                        torch.ones(1, 1).long().fill_(next_word).cuda()], dim=1)
        ys = ys.cuda()
    return ys


def low_entropy_decoding(model, max_len, sos_token, pad_token):
    ys = torch.ones(1, max_len).fill_(pad_token).long().cuda()
    ys[0, 0] = sos_token

    mask = torch.zeros(1, max_len, max_len).byte().cuda()
    
    # all tokens can look at the sos token
    mask[:, :, 0] = 1
  
    target_pos = torch.arange(max_len).unsqueeze(0).long().cuda()

    for t in range(max_len):
        out  = model.decode(ys, mask, target_pos)
        prob = model.generator(out)
        dist = torch.distributions.Categorical(prob.squeeze().exp())

        entropies = dist.entropy()                
        # zero-out words that have already been generated
        mask_t = (ys != pad_token).squeeze()
        entropies.masked_fill_(mask_t, 999999)

        position = entropies.argmin()
        sample = dist.sample()[position]

        # update the mask to take into account this new word
        mask[:, :, position] = 1
        ys[:, position] = sample
        
    return ys
    
