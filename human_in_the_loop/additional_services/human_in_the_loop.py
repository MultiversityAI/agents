import asyncio
import uvicorn
import gradio as gr
from llama_agents import ServiceComponent, HumanService
from llama_agents.message_queues.rabbitmq import RabbitMQMessageQueue
from human_in_the_loop.utils import load_from_env
from human_in_the_loop.apps.gradio_app import HumanInTheLoopGradioApp
from typing import Any, Dict
import logging

logger = logging.getLogger("human_in_the_loop")
logging.basicConfig(level=logging.INFO)

control_plane_host = load_from_env("CONTROL_PLANE_HOST")
control_plane_port = load_from_env("CONTROL_PLANE_PORT")
message_queue_host = load_from_env("RABBITMQ_HOST")
message_queue_port = load_from_env("RABBITMQ_NODE_PORT")
message_queue_username = load_from_env("RABBITMQ_DEFAULT_USER")
message_queue_password = load_from_env("RABBITMQ_DEFAULT_PASS")
human_in_the_loop_host = load_from_env("HUMAN_IN_THE_LOOP_HOST")
human_in_the_loop_port = load_from_env("HUMAN_IN_THE_LOOP_PORT")
localhost = load_from_env("LOCALHOST")


# # human in the loop function
human_input_request_queue: asyncio.Queue[Dict[str, str]] = asyncio.Queue()
human_input_result_queue: asyncio.Queue[str] = asyncio.Queue()


async def human_input_fn(prompt: str, task_id: str, **kwargs: Any) -> str:
    logger.info("human input fn invoked.")
    await human_input_request_queue.put({"prompt": prompt, "task_id": task_id})
    logger.info("placed new prompt in queue.")

    # poll until human answer is stored
    async def _poll_for_human_input_result() -> str:
        return await human_input_result_queue.get()

    try:
        human_input = await asyncio.wait_for(
            _poll_for_human_input_result(),
            timeout=6000,
        )
        logger.info(f"Recieved human input: {human_input}")
    except (
        asyncio.exceptions.TimeoutError,
        asyncio.TimeoutError,
        TimeoutError,
    ):
        logger.info(f"Timeout reached for tool_call with prompt {prompt}")
        human_input = "Something went wrong."

    return human_input


# Gradio app
gradio_app = HumanInTheLoopGradioApp(
    control_plane_host=control_plane_host,
    control_plane_port=int(control_plane_port) if control_plane_port else None,
    human_in_loop_queue=human_input_request_queue,
    human_in_loop_result_queue=human_input_result_queue,
)
gradio_app.app.queue()

# create our multi-agent framework components
message_queue = RabbitMQMessageQueue(
    url=f"amqp://{message_queue_username}:{message_queue_password}@{message_queue_host}:{message_queue_port}/"
)
human_service = HumanService(
    message_queue=message_queue,
    description="Answers queries about math.",
    host=human_in_the_loop_host,
    port=int(human_in_the_loop_port) if human_in_the_loop_port else None,
    fn_input=human_input_fn,
    human_input_prompt="{input_str}",
)
human_component = ServiceComponent.from_service_definition(human_service)

app = gr.mount_gradio_app(human_service._app, gradio_app.app, path="/gradio")


# launch
async def launch() -> None:
    # register to message queue
    start_consuming_callable = await human_service.register_to_message_queue()
    hs_task = asyncio.create_task(start_consuming_callable())  # noqa: F841

    final_tasks_consuming_callable = await message_queue.register_consumer(
        gradio_app._final_task_consumer
    )
    ft_task = asyncio.create_task(final_tasks_consuming_callable())  # noqa: F841

    cfg = uvicorn.Config(
        app,
        host=localhost,
        port=human_service.port,
    )
    server = uvicorn.Server(cfg)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(launch())