import numpy as np
import torch as t
import torch.nn as nn
import torch.nn.functional as F
from models.bottleneck import BottleneckBlock, NoBottleneck, Bottleneck
from models.resnet import ResConv1DBlock, Resnet1D
from models.encdec import Encoder, Decoder, EncoderConvBlock


# helper functions
def assert_shape(x, exp_shape):
    assert x.shape == exp_shape, f"Expected {exp_shape} got {x.shape}"


def calculate_strides(strides, downs):
    return [stride ** down for stride, down in zip(strides, downs)]


def average_metrics(_metrics):
    metrics = {}
    for _metric in _metrics:
        for key, val in _metric.items():
            if key not in metrics:
                metrics[key] = []
            metrics[key].append(val)
    return {key: sum(vals)/len(vals) for key, vals in metrics.items()}


def _loss_fn(loss_fn, x_target, x_pred, hps):
    if loss_fn == 'l1':
        return t.mean(t.abs(x_pred - x_target)) / hps.bandwidth['l1']
    elif loss_fn == 'l2':
        return t.mean((x_pred - x_target) ** 2) / hps.bandwidth['l2']
    elif loss_fn == 'linf':
        residual = ((x_pred - x_target) ** 2).reshape(x_target.shape[0], -1)
        values, _ = t.topk(residual, hps.linf_k, dim=1)
        return t.mean(values) / hps.bandwidth['l2']
    elif loss_fn == 'lmix':
        loss = 0.0
        if hps.lmix_l1:
            loss += hps.lmix_l1 * _loss_fn('l1', x_target, x_pred, hps)
        if hps.lmix_l2:
            loss += hps.lmix_l2 * _loss_fn('l2', x_target, x_pred, hps)
        if hps.lmix_linf:
            loss += hps.lmix_linf * _loss_fn('linf', x_target, x_pred, hps)
        return loss
    else:
        assert False, f"Unknown loss_fn {loss_fn}"


class VQVAE(nn.Module):
    def __init__(self, config, multipliers=None, **block_kwargs):
        super().__init__()
        self.cfg = config.vqvae
        self.commit = self.cfg.commit
        self.spectral = self.cfg.spectral

        self.downsamples = calculate_strides(self.cfg.strides_t, self.cfg.downs_t)
        self.hop_lengths = np.cumprod(self.downsamples)

        self.sample_length = config.dataset.max_path_nums
        self.x_channels = config.dataset.x_channels
        self.x_shape = (config.dataset.max_path_nums, config.dataset.x_channels)

        if multipliers is None:
            self.multipliers = [1] * self.cfg.levels
        else:
            assert len(multipliers) == self.cfg.levels, "Invalid number of multipliers"
            self.multipliers = multipliers

        self.z_shapes = [(self.x_shape[0] // self.hop_lengths[level],) for level in range(self.cfg.levels)]

        # define encoder and decoder
        self.encoders = nn.ModuleList()
        self.decoders = nn.ModuleList()

        def _block_kwargs(level):
            this_block_kwargs = dict(block_kwargs)
            this_block_kwargs["width"] *= self.multipliers[level]
            this_block_kwargs["depth"] *= self.multipliers[level]
            return this_block_kwargs
        
        def encoder(level): 
            return Encoder(
                self.x_channels, self.cfg.emb_width, level + 1,
                self.cfg.downs_t[:level+1], self.cfg.strides_t[:level+1], **_block_kwargs(level)
            )
        
        def decoder(level): 
            return Decoder(
                self.x_channels, self.cfg.emb_width, level + 1,
                self.cfg.downs_t[:level+1], self.cfg.strides_t[:level+1], **_block_kwargs(level)
            )

        for level in range(self.cfg.levels):
            self.encoders.append(encoder(level))
            self.decoders.append(decoder(level))

        # define bottleneck
        if self.cfg.use_bottleneck:
            self.bottleneck = Bottleneck(self.cfg.l_bins, self.cfg.emb_width, self.cfg.mu, self.cfg.levels)
        else:
            self.bottleneck = NoBottleneck(self.cfg.levels)    

    def _decode(self, zs, start_level=0, end_level=None):
        # Decode
        if end_level is None:
            end_level = self.levels
        assert len(zs) == end_level - start_level
        xs_quantised = self.bottleneck.decode(
            zs, start_level=start_level, end_level=end_level
        )
        assert len(xs_quantised) == end_level - start_level

        # Use only lowest level
        decoder, x_quantised = self.decoders[start_level], xs_quantised[0:1]
        x_out = decoder(x_quantised, all_levels=False)
        x_out = self.postprocess(x_out)
        return x_out

    def decode(self, zs, start_level=0, end_level=None, bs_chunks=1):
        z_chunks = [t.chunk(z, bs_chunks, dim=0) for z in zs]
        x_outs = []
        for i in range(bs_chunks):
            zs_i = [z_chunk[i] for z_chunk in z_chunks]
            x_out = self._decode(
                zs_i, start_level=start_level, end_level=end_level)
            x_outs.append(x_out)
        return t.cat(x_outs, dim=0)

    def _encode(self, x, start_level=0, end_level=None):
        # Encode
        if end_level is None:
            end_level = self.levels
        x_in = self.preprocess(x)
        xs = []
        for level in range(self.levels):
            encoder = self.encoders[level]
            x_out = encoder(x_in)
            xs.append(x_out[-1])
        zs = self.bottleneck.encode(xs)
        return zs[start_level:end_level]

    def encode(self, x, start_level=0, end_level=None, bs_chunks=1):
        x_chunks = t.chunk(x, bs_chunks, dim=0)
        zs_list = []
        for x_i in x_chunks:
            zs_i = self._encode(
                x_i, start_level=start_level, end_level=end_level)
            zs_list.append(zs_i)
        zs = [t.cat(zs_level_list, dim=0) for zs_level_list in zip(*zs_list)]
        return zs

    def sample(self, n_samples):
        zs = [t.randint(0, self.l_bins, size=(n_samples, *z_shape),
                        device='cuda') for z_shape in self.z_shapes]
        return self.decode(zs)

    def forward(self, x, hps, loss_fn='l1'):
        metrics = {}
        # Encode/Decode
        x_in = x.permute(0, 2, 1).float()
        xs = []

        for level in range(self.levels):
            encoder = self.encoders[level]
            x_out = encoder(x_in)
            xs.append(x_out[-1])

        zs, xs_quantised, commit_losses, quantiser_metrics = self.bottleneck(xs)

        x_outs = []
        for level in range(self.levels):
            decoder = self.decoders[level]
            x_out = decoder(xs_quantised[level:level+1], all_levels=False)

            # happens when deploying
            if (x_out.shape != x_in.shape):
                x_out = F.pad(
                    input=x_out, 
                    pad=(0, x_in.shape[-1]-x_out.shape[-1]), 
                    mode='constant', 
                    value=0
                )

            assert_shape(x_out, x_in.shape)
            x_outs.append(x_out)


        recons_loss = t.zeros(()).to(x.device)
        x_target = x.float()

        for level in reversed(range(self.levels)):
            x_out = x_outs[level].permute(0, 2, 1).float()
            this_recons_loss = _loss_fn(loss_fn, x_target, x_out, hps)
            metrics[f'recons_loss_l{level + 1}'] = this_recons_loss
            recons_loss += this_recons_loss

        commit_loss = sum(commit_losses)
        loss = recons_loss + self.commit * commit_loss

        with t.no_grad():
            l2_loss = _loss_fn("l2", x_target, x_out, hps)
            l1_loss = _loss_fn("l1", x_target, x_out, hps)
            linf_loss = _loss_fn("linf", x_target, x_out, hps)

        quantiser_metrics = average_metrics(quantiser_metrics)

        metrics.update(dict(
            recons_loss=recons_loss,
            l2_loss=l2_loss,
            l1_loss=l1_loss,
            linf_loss=linf_loss,
            commit_loss=commit_loss,
            **quantiser_metrics))

        for key, val in metrics.items():
            metrics[key] = val.detach()

        return x_out, loss, metrics


