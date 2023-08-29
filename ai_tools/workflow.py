from typing import NamedTuple, Tuple, Union, Optional

from .image import Bounds, Extent, Image, ImageCollection, Mask
from .client import Client
from .settings import settings
from .comfyworkflow import ComfyWorkflow, Output
from .util import compute_batch_size


Inputs = Union[Extent, Image, Tuple[Image, Mask]]


class ScaledExtent(NamedTuple):
    initial: Extent
    target: Extent
    scale: float


class ScaledInputs(NamedTuple):
    image: Optional[Image]
    mask_image: Optional[Image]
    extent: ScaledExtent
    batch_size: int

    @staticmethod
    def init(
        image: Optional[Image],
        mask: Optional[Image],
        initial: Extent,
        target: Extent,
        scale: float,
    ):
        return ScaledInputs(
            image,
            mask,
            ScaledExtent(initial.multiple_of(8), target, scale),
            compute_batch_size(target, settings.min_image_size, settings.batch_size),
        )


def prepare(inputs: Inputs, downscale=True) -> ScaledInputs:
    input_is_masked_image = isinstance(inputs, tuple) and isinstance(inputs[0], Image)
    image = inputs[0] if input_is_masked_image else None
    image = inputs if isinstance(inputs, Image) else image
    extent = inputs if isinstance(inputs, Extent) else image.extent
    mask = inputs[1] if input_is_masked_image else None
    mask_image = mask.to_image(extent) if mask else None

    min_size = settings.min_image_size
    max_size = settings.max_image_size

    if downscale and (extent.width > max_size or extent.height > max_size):
        # Image is larger than max size that diffusion can comfortably handle:
        # Scale it down so the longer side is equal to max size.
        scale = max_size / max(extent.width, extent.height)
        initial = (extent * scale).multiple_of(8)
        # Images are scaled here directly to avoid encoding and processing
        # very large images in subsequent steps.
        if image:
            image = Image.scale(image, initial)
        if mask_image:
            mask_image = Image.scale(mask_image, initial)
        assert scale < 1
        return ScaledInputs.init(image, mask_image, initial, extent, scale)

    if extent.width < min_size and extent.height < min_size:
        # Image is smaller than min size for which diffusion generates reasonable
        # results. Compute a resolution where the shorter side is equal to min size.
        scale = min_size / min(extent.width, extent.height)
        initial = (extent * scale).multiple_of(8)

        assert initial.width >= min_size and initial.height >= min_size
        assert scale > 1
        return ScaledInputs.init(image, mask_image, initial, extent, scale)

    # Image is in acceptable range, only make sure it's a multiple of 8.
    return ScaledInputs.init(image, mask_image, extent, extent, 1.0)


def _ksampler_params(clip_vision=False, upscale=False):
    sampler_name = {
        "DDIM": "ddim",
        "DPM++ 2M SDE": "dpmpp_2m_sde_gpu",
        "DPM++ 2M SDE Karras": "dpmpp_2m_sde_gpu",
    }[settings.sampler]
    sampler_scheduler = {
        "DDIM": "ddim_uniform",
        "DPM++ 2M SDE": "normal",
        "DPM++ 2M SDE Karras": "karras",
    }[settings.sampler]
    params = dict(
        sampler=sampler_name,
        scheduler=sampler_scheduler,
        steps=settings.sampler_steps,
        cfg=settings.cfg_scale,
    )
    if clip_vision:
        params["cfg"] = min(5, settings.cfg_scale)
    if upscale:
        params["steps"] = settings.sampler_steps_upscaling
    return params


def upscale_latent(
    w: ComfyWorkflow,
    latent: Output,
    target: Extent,
    prompt_pos: str,
    prompt_neg: Output,
    model: Output,
    clip: Output,
):
    assert target.is_multiple_of(8)
    upscale = w.scale_latent(latent, target)
    prompt = w.clip_text_encode(clip, f"{prompt_pos}, {settings.upscale_prompt}")
    return w.ksampler(
        model, prompt, prompt_neg, upscale, denoise=0.5, **_ksampler_params(upscale=True)
    )


def generate(input_extent: Extent, prompt: str):
    _, _, extent, batch = prepare(input_extent)

    w = ComfyWorkflow()
    model, clip, vae = w.load_checkpoint(settings.sd_checkpoint)
    latent = w.empty_latent_image(extent.initial.width, extent.initial.height, batch)
    positive = w.clip_text_encode(clip, f"{prompt}, {settings.style_prompt}")
    negative = w.clip_text_encode(clip, settings.negative_prompt)
    out_latent = w.ksampler(model, positive, negative, latent, **_ksampler_params())
    extent_sampled = extent.initial
    if extent.scale < 1:  # generated image is smaller than requested -> upscale
        extent_sampled = extent.target.multiple_of(8)
        out_latent = upscale_latent(w, out_latent, extent_sampled, prompt, negative, model, clip)
    out_image = w.vae_decode(vae, out_latent)
    if extent_sampled != extent.target:
        out_image = w.scale_image(out_image, extent.target)
    w.send_image(out_image)
    return w


def inpaint(comfy: Client, image: Image, mask: Mask, prompt: str):
    image, mask_image, extent, batch = prepare((image, mask))

    w = ComfyWorkflow()
    model, clip, vae = w.load_checkpoint(settings.sd_checkpoint)
    controlnet = w.load_controlnet(comfy.controlnet_model["inpaint"])
    clip_vision_model = w.load_clip_vision(comfy.clip_vision_model)
    in_image = w.load_image(image)
    in_mask = w.load_mask(mask_image)
    cropped_mask = w.load_mask(mask.to_image())
    if extent.scale > 1:
        in_image = w.scale_image(in_image, extent.initial)
        in_mask = w.scale_mask(in_mask, extent.initial)
    clip_vision = w.clip_vision_encode(clip_vision_model, in_image)
    ip_adapter = w.ip_adapter(comfy.ip_adapter_model, model, clip_vision, 0.5)
    control_image = w.inpaint_preprocessor(in_image, in_mask)
    positive = w.clip_text_encode(clip, f"{prompt}, {settings.style_prompt}")
    positive = w.apply_controlnet(positive, controlnet, control_image)
    negative = w.clip_text_encode(clip, settings.negative_prompt)
    latent = w.vae_encode_inpaint(vae, in_image, in_mask)
    latent = w.batch_latent(latent, batch)
    out_latent = w.ksampler(
        ip_adapter, positive, negative, latent, **_ksampler_params(clip_vision=True)
    )
    if extent.scale < 1:
        cropped_latent = w.crop_latent(out_latent, Bounds.scale(mask.bounds, extent.scale))
        scaled_latent = w.scale_latent(cropped_latent, mask.bounds.extent)
        no_mask = w.solid_mask(mask.bounds.extent, 1.0)
        masked_latent = w.set_latent_noise_mask(scaled_latent, no_mask)
        cropped_image = w.vae_decode(vae, cropped_latent)
        control_image_upscale = w.inpaint_preprocessor(cropped_image, cropped_mask)
        positive_upscale = w.clip_text_encode(clip, f"{prompt}, {settings.upscale_prompt}")
        positive_upscale = w.apply_controlnet(positive_upscale, controlnet, control_image_upscale)
        params = _ksampler_params(clip_vision=True, upscale=True)
        out_latent = w.ksampler(
            ip_adapter, positive_upscale, negative, masked_latent, denoise=0.5, **params
        )
    else:
        out_latent = w.crop_latent(out_latent, mask.bounds)
    out_image = w.vae_decode(vae, out_latent)
    if extent.scale > 1:
        out_image = w.scale_image(out_image, extent.target)
    out_masked = w.apply_mask(out_image, cropped_mask)
    w.send_image(out_masked)
    return w


def refine(image: Image, prompt: str, strength: float):
    assert strength > 0 and strength < 1
    image, _, extent, batch = prepare(image, downscale=False)

    w = ComfyWorkflow()
    model, clip, vae = w.load_checkpoint(settings.sd_checkpoint)
    in_image = w.load_image(image)
    if extent.initial != extent.target:
        in_image = w.scale_image(in_image, extent.initial)
    latent = w.vae_encode(vae, in_image)
    latent = w.batch_latent(latent, batch)
    positive = w.clip_text_encode(clip, f"{prompt} {settings.style_prompt}")
    negative = w.clip_text_encode(clip, settings.negative_prompt)
    sampler = w.ksampler(model, positive, negative, latent, denoise=strength, **_ksampler_params())
    out_image = w.vae_decode(vae, sampler)
    if extent.initial != extent.target:
        out_image = w.scale_image(out_image, extent.target)
    w.send_image(out_image)
    return w


def refine_region(comfy: Client, image: Image, mask: Mask, prompt: str, strength: float):
    assert strength > 0 and strength < 1
    assert mask.bounds.extent.is_multiple_of(8)
    downscale_if_needed = strength >= 0.7
    image = Image.sub_region(image, mask.bounds)
    image, mask_image, extent, batch = prepare((image, mask), downscale_if_needed)

    w = ComfyWorkflow()
    model, clip, vae = w.load_checkpoint(settings.sd_checkpoint)
    in_image = w.load_image(image)
    in_mask = w.load_mask(mask_image)
    if extent.scale > 1:
        in_image = w.scale_image(in_image, extent.initial)
        in_mask = w.scale_mask(in_mask, extent.initial)
    latent = w.vae_encode(vae, in_image)
    latent = w.set_latent_noise_mask(latent, in_mask)
    latent = w.batch_latent(latent, batch)
    controlnet = w.load_controlnet(comfy.controlnet_model["inpaint"])
    control_image = w.inpaint_preprocessor(in_image, in_mask)
    positive = w.clip_text_encode(clip, f"{prompt} {settings.style_prompt}")
    positive = w.apply_controlnet(positive, controlnet, control_image)
    negative = w.clip_text_encode(clip, settings.negative_prompt)
    out_latent = w.ksampler(
        model, positive, negative, latent, denoise=strength, **_ksampler_params()
    )
    if extent.scale < 1:
        out_latent = upscale_latent(w, out_latent, extent.target, prompt, negative, model, clip)
    out_image = w.vae_decode(vae, out_latent)
    if extent.scale > 1:
        out_image = w.scale_image(out_image, extent.target)
    original_mask = w.load_mask(mask.to_image())
    out_masked = w.apply_mask(out_image, original_mask)
    w.send_image(out_masked)
    return w
