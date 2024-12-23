## Restormer: Efficient Transformer for High-Resolution Image Restoration
## Syed Waqas Zamir, Aditya Arora, Salman Khan, Munawar Hayat, Fahad Shahbaz Khan, and Ming-Hsuan Yang
## https://arxiv.org/abs/2111.09881


import torch
import torch.nn as nn
import torch.nn.functional as F
from pdb import set_trace as stx
import numbers

from einops import rearrange
from einops.layers.torch import Rearrange

from basicsr.utils.registry import ARCH_REGISTRY

import  hi_diff.archs.mae_encoder
from hi_diff.archs.mae_encoder import MaskedAutoencoderViT as mae
import math

##########################################################################
## Layer Norm

def to_3d(x):
    return rearrange(x, 'b c h w -> b (h w) c')

def to_4d(x,h,w):
    return rearrange(x, 'b (h w) c -> b c h w',h=h,w=w)

class BiasFree_LayerNorm(nn.Module):
    def __init__(self, normalized_shape):
        super(BiasFree_LayerNorm, self).__init__()
        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        normalized_shape = torch.Size(normalized_shape)

        assert len(normalized_shape) == 1

        self.weight = nn.Parameter(torch.ones(normalized_shape))
        self.normalized_shape = normalized_shape

    def forward(self, x):
        sigma = x.var(-1, keepdim=True, unbiased=False)
        return x / torch.sqrt(sigma+1e-5) * self.weight

class WithBias_LayerNorm(nn.Module):
    def __init__(self, normalized_shape):
        super(WithBias_LayerNorm, self).__init__()
        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        normalized_shape = torch.Size(normalized_shape)

        assert len(normalized_shape) == 1

        self.weight = nn.Parameter(torch.ones(normalized_shape))
        self.bias = nn.Parameter(torch.zeros(normalized_shape))
        self.normalized_shape = normalized_shape

    def forward(self, x):
        mu = x.mean(-1, keepdim=True)
        sigma = x.var(-1, keepdim=True, unbiased=False)
        return (x - mu) / torch.sqrt(sigma+1e-5) * self.weight + self.bias


class LayerNorm(nn.Module):
    def __init__(self, dim, LayerNorm_type):
        super(LayerNorm, self).__init__()
        if LayerNorm_type =='BiasFree':
            self.body = BiasFree_LayerNorm(dim)
        else:
            self.body = WithBias_LayerNorm(dim)

    def forward(self, x):
        h, w = x.shape[-2:]
        return to_4d(self.body(to_3d(x)), h, w)


# w/o shape
class LayerNorm_Without_Shape(nn.Module):
    def __init__(self, dim, LayerNorm_type):
        super(LayerNorm_Without_Shape, self).__init__()
        if LayerNorm_type =='BiasFree':
            self.body = BiasFree_LayerNorm(dim)
        else:
            self.body = WithBias_LayerNorm(dim)

    def forward(self, x):
        h, w = x.shape[-2:]
        return self.body(x)


##########################################################################
## Gated-Dconv Feed-Forward Network (GDFN)
class FeedForward(nn.Module):
    def __init__(self, dim, ffn_expansion_factor, bias, embed_dim, group):
        super(FeedForward, self).__init__()

        hidden_features = int(dim*ffn_expansion_factor)

        self.project_in = nn.Conv2d(dim, hidden_features*2, kernel_size=1, bias=bias)

        self.dwconv = nn.Conv2d(hidden_features*2, hidden_features*2, kernel_size=3, stride=1, padding=1, groups=hidden_features*2, bias=bias)

        self.project_out = nn.Conv2d(hidden_features, dim, kernel_size=1, bias=bias)

        # prior
        if group == 1:
            self.ln1 = nn.Linear(embed_dim*4, dim)
            self.ln2 = nn.Linear(embed_dim*4, dim)

    def forward(self, x, prior=None):
        if prior is not None:
            k1 = self.ln1(prior).unsqueeze(-1).unsqueeze(-1)
            k2 = self.ln2(prior).unsqueeze(-1).unsqueeze(-1)
            x = (x * k1) + k2

        x = self.project_in(x)
        x1, x2 = self.dwconv(x).chunk(2, dim=1)
        x = F.gelu(x1) * x2
        x = self.project_out(x)
        return x



##########################################################################
## Multi-DConv Head Transposed Self-Attention (MDTA)
class Attention(nn.Module):
    def __init__(self, dim, num_heads, bias, embed_dim, group):
        super(Attention, self).__init__()
        self.num_heads = num_heads
        self.temperature = nn.Parameter(torch.ones(num_heads, 1, 1))

        self.qkv = nn.Conv2d(dim, dim*3, kernel_size=1, bias=bias)
        self.qkv_dwconv = nn.Conv2d(dim*3, dim*3, kernel_size=3, stride=1, padding=1, groups=dim*3, bias=bias)
        self.project_out = nn.Conv2d(dim, dim, kernel_size=1, bias=bias)

        # prior
        if group == 1:
            self.ln1 = nn.Linear(embed_dim*4, dim)
            self.ln2 = nn.Linear(embed_dim*4, dim)

    def forward(self, x, prior=None):
        b,c,h,w = x.shape
        if prior is not None:
            k1 = self.ln1(prior).unsqueeze(-1).unsqueeze(-1)
            k2 = self.ln2(prior).unsqueeze(-1).unsqueeze(-1)
            x = (x * k1) + k2

        qkv = self.qkv_dwconv(self.qkv(x))
        q,k,v = qkv.chunk(3, dim=1)   
        
        q = rearrange(q, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        k = rearrange(k, 'b (head c) h w -> b head c (h w)', head=self.num_heads)
        v = rearrange(v, 'b (head c) h w -> b head c (h w)', head=self.num_heads)

        q = torch.nn.functional.normalize(q, dim=-1)
        k = torch.nn.functional.normalize(k, dim=-1)

        attn = (q @ k.transpose(-2, -1)) * self.temperature
        attn = attn.softmax(dim=-1)

        out = (attn @ v)
        
        out = rearrange(out, 'b head c (h w) -> b (head c) h w', head=self.num_heads, h=h, w=w)

        out = self.project_out(out)
        return out


##########################################################################
## Hierarchical Integration Module
class HIM(nn.Module):
    def __init__(self, dim, num_heads, bias, embed_dim, LayerNorm_type, qk_scale=None):
        super(HIM, self).__init__()

        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim**-0.5

        self.norm1 = LayerNorm_Without_Shape(dim, LayerNorm_type)
        self.norm2 = LayerNorm_Without_Shape(embed_dim*4, LayerNorm_type)

        self.q = nn.Linear(dim, dim, bias=bias)
        self.kv = nn.Linear(embed_dim*4, 2*dim, bias=bias)
        
        self.proj = nn.Linear(dim, dim, bias=True)

    def forward(self, x, prior):
        B, C, H, W = x.shape
        x = rearrange(x, 'b c h w -> b (h w) c')
        _x = self.norm1(x)
        prior = self.norm2(prior)
        
        q = self.q(_x)
        kv = self.kv(prior)
        k,v = kv.chunk(2, dim=-1)   

        q = rearrange(q, 'b n (head c) -> b head n c', head=self.num_heads)
        k = rearrange(k, 'b n (head c) -> b head n c', head=self.num_heads)
        v = rearrange(v, 'b n (head c) -> b head n c', head=self.num_heads)

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)

        out = (attn @ v)
        out = rearrange(out, 'b head n c -> b n (head c)', head=self.num_heads)
        out = self.proj(out)

        # sum
        x = x + out
        x = rearrange(x, 'b (h w) c -> b c h w', h=H, w=W).contiguous()

        return x


##########################################################################
class TransformerBlock(nn.Module):
    def __init__(self, dim, num_heads, ffn_expansion_factor, bias, LayerNorm_type, embed_dim, group):
        super(TransformerBlock, self).__init__()

        self.norm1 = LayerNorm(dim, LayerNorm_type)
        self.attn = Attention(dim, num_heads, bias, embed_dim, group)
        self.norm2 = LayerNorm(dim, LayerNorm_type)
        self.ffn = FeedForward(dim, ffn_expansion_factor, bias, embed_dim, group)

    def forward(self, x, prior=None):
        x = x + self.attn(self.norm1(x), prior)
        x = x + self.ffn(self.norm2(x), prior)

        return x



##########################################################################
## Overlapped image patch embedding with 3x3 Conv
class OverlapPatchEmbed(nn.Module):
    def __init__(self, in_c=3, embed_dim=48, bias=False):
        super(OverlapPatchEmbed, self).__init__()

        self.proj = nn.Conv2d(in_c, embed_dim, kernel_size=3, stride=1, padding=1, bias=bias)

    def forward(self, x):
        x = self.proj(x)

        return x



##########################################################################
## Resizing modules
class Downsample(nn.Module):
    def __init__(self, n_feat):
        super(Downsample, self).__init__()

        self.body = nn.Sequential(nn.Conv2d(n_feat, n_feat//2, kernel_size=3, stride=1, padding=1, bias=False),
                                  nn.PixelUnshuffle(2))

    def forward(self, x):
        return self.body(x)

class Upsample(nn.Module):
    def __init__(self, n_feat):
        super(Upsample, self).__init__()

        self.body = nn.Sequential(nn.Conv2d(n_feat, n_feat*2, kernel_size=3, stride=1, padding=1, bias=False),
                                  nn.PixelShuffle(2))

    def forward(self, x):
        return self.body(x)


class BasicLayer(nn.Module):
    def __init__(self, dim, num_heads, ffn_expansion_factor, bias, LayerNorm_type, embed_dim, num_blocks, group):

        super().__init__()
        self.group = group

        # build blocks
        self.blocks = nn.ModuleList([TransformerBlock(dim=dim, num_heads=num_heads, ffn_expansion_factor=ffn_expansion_factor,
                                    bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, group=group) for i in range(num_blocks)])
        if self.group > 1:
            self.him = HIM(dim, num_heads, bias, embed_dim, LayerNorm_type)

    def forward(self, x, prior=None):
        if prior is not None and self.group > 1:
            x = self.him(x, prior)
            prior = None

        for blk in self.blocks:
            x = blk(x, prior)
                
        return x


##########################################################################
# The implementation builds on Restormer code https://github.com/swz30/Restormer/blob/main/basicsr/models/archs/restormer_arch.py
@ARCH_REGISTRY.register()
class Transformer(nn.Module):
    def __init__(self, 
        inp_channels=3, 
        out_channels=3, 
        dim = 48,
        num_blocks = [4,6,6,8], 
        num_refinement_blocks = 4,
        heads = [1,2,4,8],
        ffn_expansion_factor = 2.66,
        bias = False,
        LayerNorm_type = 'WithBias',   ## Other option 'BiasFree'
        dual_pixel_task = False,        ## True for dual-pixel defocus deblurring only. Also set inp_channels=6
        embed_dim = 48,         # 匹配 MAE encoder的 embed_dim 原来为48  可能改为1024  存疑  ,实际上发现MAE的输入通道为3
        group=4,
        mae_weights_path=None
    ):

        super(Transformer, self).__init__()

        # 初始化MAE
        self.mae_encoder = mae(
            img_size=224,          # 根据需求调整
            patch_size=16,
            in_chans=inp_channels,
            embed_dim=embed_dim,
            depth=12,              # 根据需求调整
            num_heads=heads[0]
        )

        # 载入MAE预训练权重
        if mae_weights_path is not None:
            self.load_mae_weights(mae_weights_path)

        # 初始化用于调整MAE输出的层
        self.linear_layer = nn.Linear(50, 14 * 14)  # 调整维度为 14x14
        self.channel_expand_layer = nn.Conv2d(64, 384, kernel_size=1, stride=1)  # 增加通道数
        self.conv_layer = nn.Conv2d(384, 384, kernel_size=3, stride=1, padding=1)  # 卷积层
        self.upsample_layer = nn.Upsample(size=(28, 28), mode="bilinear", align_corners=False)  # 上采样

        self.channel_increase_layer_level3 = nn.Conv2d(192, 384, kernel_size=1)  # 修改为 192 -> 384
        self.channel_increase_layer_level2 = nn.Conv2d(96, 192, kernel_size=1)  # 修改为 96 -> 192
        self.channel_increase_layer_level1 = nn.Conv2d(48, 96, kernel_size=1)  # 修改为 48 -> 96

        # multi-scale
        self.down_1 = nn.Sequential(
            Rearrange('b n c -> b c n'),
            nn.Linear(group*group, (group*group)//4),
            Rearrange('b c n -> b n c'),
            nn.Linear(embed_dim*4, embed_dim*4)
        )
        self.down_2 = nn.Sequential(
            Rearrange('b n c -> b c n'),
            nn.Linear((group*group)//4, 1),
            Rearrange('b c n -> b n c'),
            nn.Linear(embed_dim*4, embed_dim*4)
        )

        self.patch_embed = OverlapPatchEmbed(inp_channels, dim)

        # 【缩减到 3 通道, 简单的措施，为了匹配MAE的输入，后续可以考虑更好的办法】
        self.channel_reducer = nn.Conv2d(48, 3, kernel_size=1, stride=1, padding=0, bias=False)

        self.encoder_level1 = BasicLayer(dim=dim, num_heads=heads[0], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_blocks[0], group=group)

        self.down1_2 = Downsample(dim) ## From Level 1 to Level 2
        self.encoder_level2 = BasicLayer(dim=int(dim*2**1), num_heads=heads[1], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_blocks[1], group=group//2)

        self.down2_3 = Downsample(int(dim*2**1)) ## From Level 2 to Level 3
        self.encoder_level3 = BasicLayer(dim=int(dim*2**2), num_heads=heads[2], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_blocks[2], group=group//2)

        self.down3_4 = Downsample(int(dim*2**2)) ## From Level 3 to Level 4
        self.latent = BasicLayer(dim=int(dim*2**3), num_heads=heads[3], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_blocks[3], group=1)

        self.up4_3 = Upsample(int(dim*2**3)) ## From Level 4 to Level 3
        self.reduce_chan_level3 = nn.Conv2d(int(dim*2**3), int(dim*2**2), kernel_size=1, bias=bias)
        self.decoder_level3 = BasicLayer(dim=int(dim*2**2), num_heads=heads[2], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_blocks[2], group=group//2)


        self.up3_2 = Upsample(int(dim*2**2)) ## From Level 3 to Level 2
        self.reduce_chan_level2 = nn.Conv2d(int(dim*2**2), int(dim*2**1), kernel_size=1, bias=bias)
        self.decoder_level2 = BasicLayer(dim=int(dim*2**1), num_heads=heads[1], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_blocks[1], group=group//2)

        self.up2_1 = Upsample(int(dim*2**1))  ## From Level 2 to Level 1  (NO 1x1 conv to reduce channels)

        self.decoder_level1 = BasicLayer(dim=int(dim*2**1), num_heads=heads[0], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_blocks[0], group=group)

        self.refinement = BasicLayer(dim=int(dim*2**1), num_heads=heads[0], ffn_expansion_factor=ffn_expansion_factor, bias=bias, LayerNorm_type=LayerNorm_type, embed_dim=embed_dim, num_blocks=num_refinement_blocks, group=group)

        #### For Dual-Pixel Defocus Deblurring Task ####
        self.dual_pixel_task = dual_pixel_task
        if self.dual_pixel_task:
            self.skip_conv = nn.Conv2d(dim, int(dim*2**1), kernel_size=1, bias=bias)
        ###########################
            
        self.output = nn.Conv2d(int(dim*2**1), out_channels, kernel_size=3, stride=1, padding=1, bias=bias)

    def load_mae_weights(self, mae_weights_path):
        """加载MAE预训练权重."""
        pretrained_dict = torch.load(mae_weights_path, map_location=None)
        self.mae_encoder.load_state_dict(pretrained_dict, strict=False)
        print(f"Loaded MAE encoder weights from {mae_weights_path}")

    def forward(self, inp_img, prior=None):
        
        # # 【新增，更改输入图片的大小224x224，符合MAE的输入】
        # inp_img = F.interpolate(inp_img, size=(224, 224), mode='bilinear', align_corners=False)

        # multi-scale prior
        prior_1 = prior
        prior_2 = self.down_1(prior_1)
        prior_3 = self.down_2(prior_2).flatten(1)

        # 将下面注释的部分，替换为直接使用MAE的权重
        # inp_enc_level1 = self.patch_embed(inp_img)
        # out_enc_level1 = self.encoder_level1(inp_enc_level1, prior_1)
        
        # inp_enc_level2 = self.down1_2(out_enc_level1)
        # out_enc_level2 = self.encoder_level2(inp_enc_level2, prior_2)

        # inp_enc_level3 = self.down2_3(out_enc_level2)
        # out_enc_level3 = self.encoder_level3(inp_enc_level3, prior_2) 

        # inp_enc_level4 = self.down3_4(out_enc_level3)        
        # latent = self.latent(inp_enc_level4, prior_3) # [4, 384, 28, 28]

        # inp_dec_level3 = self.up4_3(latent)  # [4, 192, 56, 56]   
        # inp_dec_level3 = torch.cat([inp_dec_level3, out_enc_level3], 1)  # [4, 384, 56, 56]
        # inp_dec_level3 = self.reduce_chan_level3(inp_dec_level3)    # [4, 192, 56, 56]
        # out_dec_level3 = self.decoder_level3(inp_dec_level3, prior_2)   # [4, 192, 56, 56]

        # inp_dec_level2 = self.up3_2(out_dec_level3)     # [4, 96, 112, 112]
        # inp_dec_level2 = torch.cat([inp_dec_level2, out_enc_level2], 1)     # [4, 192, 112, 112]
        # inp_dec_level2 = self.reduce_chan_level2(inp_dec_level2)    # [4, 96, 112, 112]
        # out_dec_level2 = self.decoder_level2(inp_dec_level2, prior_2)   # [4, 96, 112, 112]

        # inp_dec_level1 = self.up2_1(out_dec_level2)     # [4, 48, 224, 224]
        # inp_dec_level1 = torch.cat([inp_dec_level1, out_enc_level1], 1)     # [4, 96, 224, 224]
        # out_dec_level1 = self.decoder_level1(inp_dec_level1, prior_1)   # [4, 96, 224, 224]
        
        # out_dec_level1 = self.refinement(out_dec_level1, prior_1)   # [4, 96, 224, 224]

        # ========== 分割线   下面是我修改的代码 ===========

        inp_enc_level1 = self.patch_embed(inp_img)

        inp_enc_level1 = self.channel_reducer(inp_enc_level1)  # 【缩减到 3 通道, 简单的措施，为了匹配MAE的输入，后续可以考虑更好的办法】

        mae_output = self.mae_encoder(inp_enc_level1)  # [B, P, C]

        # 使用线性层调整维度
        mae_output = mae_output.permute(0, 2, 1)  # [4, 64, 50] -> [4, 50, 64]
        mae_output = self.linear_layer(mae_output)  # 调整为 [4, 196, 64]
        mae_output = mae_output.view(mae_output.size(0), 64, 14, 14)  # 调整为 [4, 64, 14, 14]

        # 使用通道扩展层调整通道数
        mae_output = self.channel_expand_layer(mae_output)  # [4, 64, 14, 14] -> [4, 384, 14, 14]

        # 使用卷积层和上采样
        mae_output = self.conv_layer(mae_output)  # [4, 384, 14, 14]
        mae_output = self.upsample_layer(mae_output)  # [4, 384, 28, 28]

        latent = self.latent(mae_output, prior_3)   # [4, 384, 28, 28]

        inp_dec_level3 = self.up4_3(latent)     # [4, 192, 56, 56]
        inp_dec_level3 = self.channel_increase_layer_level3(inp_dec_level3)  # [4, 384, 56, 56]
        inp_dec_level3 = self.reduce_chan_level3(inp_dec_level3)    # [4, 192, 56, 56]
        out_dec_level3 = self.decoder_level3(inp_dec_level3, prior_2) # [4, 192, 56, 56]

        inp_dec_level2 = self.up3_2(out_dec_level3)     # [4, 96, 112, 112]             未检验
        inp_dec_level2 = self.channel_increase_layer_level2(inp_dec_level2)    # [4, 192, 112, 112]
        inp_dec_level2 = self.reduce_chan_level2(inp_dec_level2)      # [4, 96, 112, 112]
        out_dec_level2 = self.decoder_level2(inp_dec_level2, prior_2) # [4, 96, 112, 112]

        inp_dec_level1 = self.up2_1(out_dec_level2)     # [4, 48, 224, 224]
        inp_dec_level1 = self.channel_increase_layer_level1(inp_dec_level1)    # [4, 96, 224, 224]
        out_dec_level1 = self.decoder_level1(inp_dec_level1, prior_1)   # [4, 96, 224, 224]
        
        out_dec_level1 = self.refinement(out_dec_level1, prior_1)   # [4, 96, 224, 224]

        # ========== 分割线   上面是我修改的代码 ===========

        #### For Dual-Pixel Defocus Deblurring Task ####
        if self.dual_pixel_task:
            out_dec_level1 = out_dec_level1 + self.skip_conv(inp_enc_level1)
            out_dec_level1 = self.output(out_dec_level1)
        ###########################
        else:
            out_dec_level1 = self.output(out_dec_level1) + inp_img

        return out_dec_level1
