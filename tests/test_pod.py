from pathlib import Path
import pytest
import json
import subprocess
import os
import sys
import asyncio
import aiohttp

from ai_diffusion.api import (
    WorkflowInput,
    WorkflowKind,
    ControlInput,
    ExtentInput,
    ImageInput,
    CheckpointInput,
    SamplingInput,
    TextInput,
)
from ai_diffusion.client import Client, ClientMessage, ClientEvent
from ai_diffusion.cloud_client import CloudClient
from ai_diffusion.image import Extent, Image
from ai_diffusion.resources import ControlMode
from ai_diffusion.util import ensure
from .config import root_dir, test_dir, result_dir

pod_main = root_dir / "service" / "pod" / "pod.py"
run_dir = test_dir / "pod"


@pytest.fixture(scope="module")
def pod_server(qtapp, pytestconfig):
    async def serve(process: asyncio.subprocess.Process):
        try:
            async for line in ensure(process.stdout):
                print(line.decode("utf-8"), end="")
        except asyncio.CancelledError:
            process.terminate()
            await process.wait()

    async def start():
        env = os.environ.copy()
        env["COMFYUI_DIR"] = "C:\\Dev\\ComfyUI"
        args = ["-u", "-Xutf8", str(pod_main), "--rp_serve_api"]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        async for line in ensure(process.stdout):
            text = line.decode("utf-8")
            print(text[:80], end="")
            if "Uvicorn running" in text:
                break

        return process, asyncio.create_task(serve(process))

    async def stop(process, task):
        process.terminate()
        task.cancel()
        await process.communicate()

    if pytestconfig.getoption("--no-server-launch"):
        # Expects pod server is already running, eg. via docker
        yield "http://localhost:8000"
    else:
        process, task = qtapp.run(start())
        yield "http://localhost:8000"
        qtapp.run(stop(process, task))


async def receive_images(client: Client, work: WorkflowInput):
    job_id = None
    job_id = await client.enqueue(work)
    async for msg in client.listen():
        if msg.event is ClientEvent.finished and msg.job_id == job_id:
            assert msg.images is not None
            return msg.images
        if msg.event is ClientEvent.error:
            raise Exception(msg.error)
    assert False, "Connection closed without receiving images"


def run_and_save(
    qtapp,
    client: Client,
    work: WorkflowInput,
    filename: str,
    output_dir: Path = result_dir,
):
    async def runner():
        return await receive_images(client, work)

    results = qtapp.run(runner())
    assert len(results) == 1
    results[0].save(output_dir / filename)
    return results[0]


def test_simple(qtapp, pod_server):
    workflow = WorkflowInput(
        WorkflowKind.generate,
        images=ImageInput.from_extent(Extent(512, 512)),
        models=CheckpointInput("dreamshaper_8.safetensors"),
        sampling=SamplingInput("DPM++ 2M", 20, 5.0),
        text=TextInput("fluffy ball"),
    )

    client = qtapp.run(CloudClient.connect(pod_server, dev_mode=True))
    run_and_save(qtapp, client, workflow, "pod_simple.png")
