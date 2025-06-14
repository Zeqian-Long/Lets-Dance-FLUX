import math
from typing import Callable

import torch
from einops import rearrange, repeat
from torch import Tensor
import matplotlib.pyplot as plt
import numpy as np
import torch.nn.functional as F

from .model import Flux
from .modules.conditioner import HFEmbedder

from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from tqdm import tqdm
from scipy.signal import convolve2d
from scipy.ndimage import gaussian_filter
import os


def prepare(t5: HFEmbedder, clip: HFEmbedder, img: Tensor, prompt: str | list[str]) -> dict[str, Tensor]:
    bs, c, h, w = img.shape
    if bs == 1 and not isinstance(prompt, str):
        bs = len(prompt)

    img = rearrange(img, "b c (h ph) (w pw) -> b (h w) (c ph pw)", ph=2, pw=2)
    if img.shape[0] == 1 and bs > 1:
        img = repeat(img, "1 ... -> bs ...", bs=bs)

    img_ids = torch.zeros(h // 2, w // 2, 3)
    img_ids[..., 1] = img_ids[..., 1] + torch.arange(h // 2)[:, None]
    img_ids[..., 2] = img_ids[..., 2] + torch.arange(w // 2)[None, :]
    img_ids = repeat(img_ids, "h w c -> b (h w) c", b=bs)

    if isinstance(prompt, str):
        prompt = [prompt]
    txt = t5(prompt)
    if txt.shape[0] == 1 and bs > 1:
        txt = repeat(txt, "1 ... -> bs ...", bs=bs)
    txt_ids = torch.zeros(bs, txt.shape[1], 3)

    vec = clip(prompt)
    if vec.shape[0] == 1 and bs > 1:
        vec = repeat(vec, "1 ... -> bs ...", bs=bs)

    return {
        "img": img,
        "img_ids": img_ids.to(img.device),
        "txt": txt.to(img.device),
        "txt_ids": txt_ids.to(img.device),
        "vec": vec.to(img.device),
    }


def time_shift(mu: float, sigma: float, t: Tensor):
    return math.exp(mu) / (math.exp(mu) + (1 / t - 1) ** sigma)


def get_lin_function(
    x1: float = 256, y1: float = 0.5, x2: float = 4096, y2: float = 1.15
) -> Callable[[float], float]:
    m = (y2 - y1) / (x2 - x1)
    b = y1 - m * x1
    return lambda x: m * x + b


def get_schedule(
    num_steps: int,
    image_seq_len: int,
    base_shift: float = 0.5,
    max_shift: float = 1.15,
    shift: bool = True,
) -> list[float]:
    # extra step for zero
    timesteps = torch.linspace(1, 0, num_steps + 1)

    # shifting the schedule to favor high timesteps for higher signal images
    if shift:
        # estimate mu based on linear estimation between two points
        mu = get_lin_function(y1=base_shift, y2=max_shift)(image_seq_len)
        timesteps = time_shift(mu, 1.0, timesteps)

    return timesteps.tolist()


def build_inject_list(num_inference_steps: int, inject_step: int, tail_pad: int = 0, front_pad: int = 0):
    total = num_inference_steps - 1
    available_middle = total - front_pad - tail_pad

    if inject_step > available_middle:
        raise ValueError(f"inject_step {inject_step} is too large. Only {available_middle} steps available between front_pad and tail_pad.")

    middle_false = available_middle - inject_step
    middle_list = [False] * middle_false + [True] * inject_step

    inject_list = [True] * front_pad + middle_list + [False] * tail_pad
    return inject_list


def denoise(
    model: Flux,
    # model input
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    vec: Tensor,
    # sampling parameters
    timesteps: list[float],
    inverse,
    info: dict = None,
    inject_list: list[bool] = None, 
    guidance: float = 4.0,
    controlnet=None,                  
    control_patch=None,             
    controlnet_scale: Union[float, list[float]] = 1.0,
    controlnet_mode: Union[int, list[int]] = 0,
    guidance_start: float = 0.0, 
    guidance_end: float = 1.0
):
    # this is ignored for schnell

    if inverse:
        timesteps = timesteps[::-1]
        inject_list = inject_list[::-1]

    print(inject_list)

    guidance_vec = torch.full((img.shape[0],), guidance, device=img.device, dtype=img.dtype)

    if info is not None:
        info['inv_noise'] = {}
        info['map'] = {}
        info['edit_map'] = None

    desc = "Inversion" if inverse else "Denoising"

    for i, (t_curr, t_prev) in tqdm(enumerate(zip(timesteps[:-1], timesteps[1:])), desc=desc, total=len(timesteps) - 1):
        t_vec = torch.full((img.shape[0],), t_curr, dtype=img.dtype, device=img.device)
        info['t'] = t_prev if inverse else t_curr
        info['inverse'] = inverse
        info['second_order'] = False
        info['inject'] = inject_list[i]


        if controlnet is not None and control_patch is not None:
            progress = i / (len(timesteps) - 1)
            if guidance_start <= progress <= guidance_end:
                control_mode_t = controlnet_mode
                control_scale_t = controlnet_scale
                controlnet_block_samples, controlnet_single_block_samples = controlnet(
                    hidden_states=img,
                    controlnet_cond=control_patch,
                    controlnet_mode=control_mode_t,
                    conditioning_scale=control_scale_t,
                    timestep=torch.tensor([t_curr], dtype=img.dtype, device=img.device),
                    guidance=torch.tensor([guidance], dtype=img.dtype, device=img.device),
                    pooled_projections=vec,  
                    encoder_hidden_states=txt,
                    txt_ids=txt_ids[0],
                    img_ids=img_ids[0],
                    joint_attention_kwargs=None,
                    return_dict=False,
                )
            else:
                controlnet_block_samples = None
                controlnet_single_block_samples = None
        else:
            controlnet_block_samples = None
            controlnet_single_block_samples = None


        pred, info = model(
            img=img,
            img_ids=img_ids,
            txt=txt,
            txt_ids=txt_ids,
            y=vec,
            timesteps=t_vec,
            guidance=guidance_vec,
            info=info,
            controlnet_block_samples=controlnet_block_samples,
            controlnet_single_block_samples=controlnet_single_block_samples
        )


        img_mid = img + (t_prev - t_curr) / 2 * pred

        t_vec_mid = torch.full((img.shape[0],), (t_curr + (t_prev - t_curr) / 2), dtype=img.dtype, device=img.device)
        info['second_order'] = True

        if controlnet is not None and control_patch is not None:
            progress_mid = (i + 0.5) / (len(timesteps) - 1)
            if guidance_start <= progress_mid <= guidance_end:
                control_mode_t = controlnet_mode
                control_scale_t = controlnet_scale
                controlnet_block_samples_mid, controlnet_single_block_samples_mid = controlnet(
                    hidden_states=img_mid,
                    controlnet_cond=control_patch,
                    controlnet_mode=control_mode_t,
                    conditioning_scale=control_scale_t,
                    timestep=torch.tensor([t_vec_mid[0].item()], dtype=img.dtype, device=img.device),
                    guidance=torch.tensor([guidance], dtype=img.dtype, device=img.device),
                    pooled_projections=vec,
                    encoder_hidden_states=txt,
                    txt_ids=txt_ids[0],
                    img_ids=img_ids[0],
                    joint_attention_kwargs=None,
                    return_dict=False,
                )
            else:
                controlnet_block_samples_mid = None
                controlnet_single_block_samples_mid = None
        else:
            controlnet_block_samples_mid = None
            controlnet_single_block_samples_mid = None

        pred_mid, info = model(
            img=img_mid,
            img_ids=img_ids,
            txt=txt,
            txt_ids=txt_ids,
            y=vec,
            timesteps=t_vec_mid,
            guidance=guidance_vec,
            info=info,
            controlnet_block_samples=controlnet_block_samples_mid,
            controlnet_single_block_samples=controlnet_single_block_samples_mid
        )

        first_order = (pred_mid - pred) / ((t_prev - t_curr) / 2)
        img = img + (t_prev - t_curr) * pred + 0.5 * (t_prev - t_curr) ** 2 * first_order

        if inverse:
            step =  f'step{ len(timesteps) - i - 2}'
            info['inv_noise'][step] = (pred + pred_mid) / 2

    return img, info


def denoise_with_importance(
    model: Flux,
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    vec: Tensor,
    timesteps: list[float],
    inverse,
    width,
    height,
    guidance: float = 4.0,
    info: dict=None,
    inject_list: list[bool] = None,
    tail_pad: int = 1,
    front_pad: int = 3,
    controlnet=None,                  
    control_patch=None,             
    controlnet_scale: Union[float, list[float]] = 1.0,
    controlnet_mode: Union[int, list[int]] = 0,
    guidance_start: float = 0.0, 
    guidance_end: float = 1.0
):

    if inverse:
        timesteps = timesteps[::-1]
        inject_list = inject_list[::-1]

    print(inject_list)

    guidance_vec = torch.full((img.shape[0],), guidance, device=img.device, dtype=img.dtype)

    desc = "Inversion" if inverse else "Denoising"

    cut = len(inject_list) - info['inject_step'] - 2 - tail_pad

    print(f"Cutting at {cut} step")

    if info is not None:
        info['map'] = {}
        info['edit_map'] = None

    for i, (t_curr, t_prev) in tqdm(enumerate(zip(timesteps[:-1], timesteps[1:])), desc=desc, total=len(timesteps) - 1):
        # if i == 10:
        #     break
        t_vec = torch.full((img.shape[0],), t_curr, dtype=img.dtype, device=img.device)


        step =  f'step{ i}'
        pred_src = info['inv_noise'][step]

        pred_tar, _ = model(
            img=img,
            img_ids=img_ids,
            txt=txt,
            txt_ids=txt_ids,
            y=vec,
            timesteps=t_vec,
            guidance=guidance_vec,
            info=None
        )
        img_mid_test = img + (t_prev - t_curr) / 2 * pred_tar
        t_vec_mid = torch.full((img.shape[0],), (t_curr + (t_prev - t_curr) / 2), dtype=img.dtype, device=img.device)
        pred_mid_test, _ = model(
            img=img_mid_test,
            img_ids=img_ids,
            txt=txt,
            txt_ids=txt_ids,
            y=vec,
            timesteps=t_vec_mid,
            guidance=guidance_vec,
            info=None
        )
        first_order = (pred_mid_test - pred_tar) / ((t_prev - t_curr) / 2)
        pred_tar = (pred_mid_test + pred_tar) / 2


        delta = (pred_src - pred_tar).pow(2).sum(dim=-1).sqrt()

        
        delta_min = delta.min()
        delta_max = delta.max()
        delta_norm = (delta - delta_min) / (delta_max - delta_min)
        H_patch = math.ceil(height / 16)
        W_patch = math.ceil(width / 16)
        delta_map = delta_norm[0].reshape(W_patch, H_patch)

        if info is not None and i >= front_pad:
            info['map'][f"{i}_delta_map"] = delta_map


        vis_dir = info.get("vis_path", None)
        if vis_dir:
            delta_dir = os.path.join(vis_dir, "delta")
            os.makedirs(delta_dir, exist_ok=True)
            plt.imsave(os.path.join(delta_dir, f"delta_map_{i}.png"), delta_map.to(torch.float32).cpu().numpy(), cmap="viridis")



        if i == cut:
            delta_stack = torch.stack([v for k, v in info['map'].items() if k.endswith("_delta_map")], dim=0)  # [N, H_patch, W_patch]
            # np.save("delta_stack.npy", delta_stack.cpu().to(torch.float32).numpy())
            
            delta_max_map = delta_stack.max(dim=0).values  # [H_patch, W_patch]
            delta_max_map = (delta_max_map - delta_max_map.min()) / (delta_max_map.max() - delta_max_map.min()) 

            # threshold
            threshold = 0.27
            binary_map = (delta_max_map > threshold).float()

            binary_np = binary_map.cpu().numpy()

            # kernel = np.array([[1, 1, 1],
            #                     [1, 0, 1],
            #                     [1, 1, 1]])
            # neighbor_sum = convolve2d(binary_np, kernel, mode='same', boundary='fill', fillvalue=0)
            # binary_np[(binary_np == 0) & (neighbor_sum > 3)] = 1  # 超过4个白色邻居 → 变白
            # binary_map = torch.tensor(binary_np, device=binary_map.device, dtype=binary_map.dtype)


            smoothed_np = gaussian_filter(binary_np, sigma=1.2)
            smoothed_binary_np = (smoothed_np > 0.4).astype(np.uint8)
            binary_map = torch.tensor(smoothed_binary_np, device=binary_map.device, dtype=torch.float32)

            # scale = 4.0 
            # softmax_weights = F.softmax(delta_stack * scale, dim=0)  # [N, H, W]
            # soft_mask = (delta_stack * softmax_weights).sum(dim=0)  # [H, W]
            # soft_np = soft_mask.to(torch.float32).cpu().numpy()  # [H_patch, W_patch]
            # smoothed_np = gaussian_filter(soft_np, sigma=1.2)
            # threshold = 0.25
            # smoothed_binary_np = (smoothed_np > threshold).astype(np.uint8)
            # binary_map = torch.tensor(smoothed_binary_np, device=delta_stack.device, dtype=torch.float32)


            # flatten and extract foreground patch indices
            edit_map_flat = binary_map.flatten()  # [N_patch]
            edit_indices = (edit_map_flat > 0).nonzero(as_tuple=False).squeeze(1)  # [N_foreground]
            info["edit_map"] = edit_indices  


            if vis_dir:
                plt.figure()
                plt.imshow(binary_map.to(torch.float32).cpu().numpy(), cmap='viridis')
                plt.colorbar()
                plt.title("Edit Map")
                plt.savefig(os.path.join(vis_dir, "edit_map.png"))
                plt.close()
                print("Saved edit map visualization to edit_map.png")


            # plt.figure(figsize=(6, 5))
            # plt.imshow(binary_map.to(torch.float32).cpu().numpy(), cmap='viridis')
            # plt.colorbar()
            # plt.title("Edit Map")
            # plt.savefig("edit_map.png")
            # plt.close()
            # print("Saved edit map visualization to edit_map.png")




        info['t'] = t_prev if inverse else t_curr
        info['inverse'] = inverse
        info['second_order'] = False
        info['inject'] = inject_list[i]

        if controlnet is not None and control_patch is not None:
            progress = i / (len(timesteps) - 1)
            if guidance_start <= progress <= guidance_end:
                control_mode_t = controlnet_mode
                control_scale_t = controlnet_scale
                controlnet_block_samples, controlnet_single_block_samples = controlnet(
                    hidden_states=img,
                    controlnet_cond=control_patch,
                    controlnet_mode=control_mode_t,
                    conditioning_scale=control_scale_t,
                    timestep=torch.tensor([t_curr], dtype=img.dtype, device=img.device),
                    guidance=torch.tensor([guidance], dtype=img.dtype, device=img.device),
                    pooled_projections=vec,  
                    encoder_hidden_states=txt,
                    txt_ids=txt_ids[0],
                    img_ids=img_ids[0],
                    joint_attention_kwargs=None,
                    return_dict=False,
                )
            else:
                controlnet_block_samples = None
                controlnet_single_block_samples = None
        else:
            controlnet_block_samples = None
            controlnet_single_block_samples = None

        pred, info = model(
            img=img,
            img_ids=img_ids,
            txt=txt,
            txt_ids=txt_ids,
            y=vec,
            timesteps=t_vec,
            guidance=guidance_vec,
            info=info,
            controlnet_block_samples=controlnet_block_samples,
            controlnet_single_block_samples=controlnet_single_block_samples
        )

        img_mid = img + (t_prev - t_curr) / 2 * pred

        t_vec_mid = torch.full((img.shape[0],), (t_curr + (t_prev - t_curr) / 2), dtype=img.dtype, device=img.device)
        info['second_order'] = True


        if controlnet is not None and control_patch is not None:
            progress_mid = (i + 0.5) / (len(timesteps) - 1)
            if guidance_start <= progress_mid <= guidance_end:
                control_mode_t = controlnet_mode
                control_scale_t = controlnet_scale
                controlnet_block_samples_mid, controlnet_single_block_samples_mid = controlnet(
                    hidden_states=img_mid,
                    controlnet_cond=control_patch,
                    controlnet_mode=control_mode_t,
                    conditioning_scale=control_scale_t,
                    timestep=torch.tensor([t_vec_mid[0].item()], dtype=img.dtype, device=img.device),
                    guidance=torch.tensor([guidance], dtype=img.dtype, device=img.device),
                    pooled_projections=vec,
                    encoder_hidden_states=txt,
                    txt_ids=txt_ids[0],
                    img_ids=img_ids[0],
                    joint_attention_kwargs=None,
                    return_dict=False,
                )
            else:
                controlnet_block_samples_mid = None
                controlnet_single_block_samples_mid = None
        else:
            controlnet_block_samples_mid = None
            controlnet_single_block_samples_mid = None


        pred_mid, info = model(
            img=img_mid,
            img_ids=img_ids,
            txt=txt,
            txt_ids=txt_ids,
            y=vec,
            timesteps=t_vec_mid,
            guidance=guidance_vec,
            info=info,
            controlnet_block_samples=controlnet_block_samples_mid,
            controlnet_single_block_samples=controlnet_single_block_samples_mid
        )

        first_order = (pred_mid - pred) / ((t_prev - t_curr) / 2)
        img = img + (t_prev - t_curr) * pred + 0.5 * (t_prev - t_curr) ** 2 * first_order

    return img, info


def unpack(x: Tensor, height: int, width: int) -> Tensor:
    return rearrange(
        x,
        "b (h w) (c ph pw) -> b c (h ph) (w pw)",
        h=math.ceil(height / 16),
        w=math.ceil(width / 16),
        ph=2,
        pw=2,
    )
