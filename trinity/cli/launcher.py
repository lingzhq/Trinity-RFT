"""Launch the trainer"""
import argparse
import os
import sys
import traceback
from pathlib import Path
from pprint import pprint

import ray

from trinity.common.config import Config, DataPipelineConfig, load_config
from trinity.explorer.explorer import Explorer
from trinity.trainer.trainer import Trainer
from trinity.utils.log import get_logger
from trinity.utils.plugin_loader import load_plugins

logger = get_logger(__name__)


def bench(config: Config) -> None:
    """Evaluate model."""
    explorer = (
        ray.remote(Explorer)
        .options(
            name=config.explorer.name,
            namespace=ray.get_runtime_context().namespace,
        )
        .remote(config)
    )
    try:
        ray.get(explorer.prepare.remote())
        ray.get(explorer.benchmark.remote())
        logger.info("Benchmark finished.")
        ray.get(explorer.shutdown.remote())
    except Exception:
        error_msg = traceback.format_exc()
        logger.error(f"Benchmark failed:\n{error_msg}")


def explore(config: Config) -> None:
    """Run explorer."""
    try:
        explorer = (
            ray.remote(Explorer)
            .options(
                name=config.explorer.name,
                namespace=ray.get_runtime_context().namespace,
            )
            .remote(config)
        )
        ray.get(explorer.prepare.remote())
        ray.get(explorer.sync_weight.remote())
        ray.get(explorer.explore.remote())
        ray.get(explorer.shutdown.remote())
    except Exception:
        error_msg = traceback.format_exc()
        logger.error(f"Explorer failed:\n{error_msg}")


def train(config: Config) -> None:
    """Run trainer."""
    try:
        trainer = (
            ray.remote(Trainer)
            .options(
                name=config.trainer.name,
                namespace=ray.get_runtime_context().namespace,
            )
            .remote(config)
        )
        ray.get(trainer.prepare.remote())
        ray.get(trainer.sync_weight.remote())
        ray.get(trainer.train.remote())
        ray.get(trainer.shutdown.remote())
    except Exception:
        error_msg = traceback.format_exc()
        logger.error(f"Trainer failed:\n{error_msg}")


def both(config: Config) -> None:
    """Setup both explorer and trainer.

    For the explorer, a step contains `batch_size * sync_interval` number
    of rollout tasks.

    For the trainer, it has to consume all experiences generated by the explorer in
    the latest step. The specific number of experiences may vary for different
    algorithms and tasks.
    """
    namespace = ray.get_runtime_context().namespace
    explorer = (
        ray.remote(Explorer)
        .options(
            name=config.explorer.name,
            namespace=namespace,
        )
        .remote(config)
    )
    trainer = (
        ray.remote(Trainer)
        .options(
            name=config.trainer.name,
            namespace=namespace,
        )
        .remote(config)
    )
    ray.get([explorer.__ray_ready__.remote(), trainer.__ray_ready__.remote()])
    ray.get(
        [
            explorer.prepare.remote(),
            trainer.prepare.remote(),
        ]
    )
    ray.get(
        [
            explorer.sync_weight.remote(),
            trainer.sync_weight.remote(),
        ]
    )
    ready_ref, wait_ref = ray.wait(
        [
            explorer.explore.remote(),
            trainer.train.remote(),
        ],
        num_returns=1,
    )

    ready = ray.get(ready_ref[0])
    if ready == config.trainer.name:
        logger.info(
            "===========================================================\n"
            "> Launcher detected that the `Trainer` process has finished.\n"
            "> Stopping the explorer process immediately.\n"
            "==========================================================="
        )
        ray.wait(wait_ref, timeout=5)
    elif ready == config.explorer.name:
        logger.info(
            "============================================================\n"
            "> Launcher detected that the `Explorer` process has finished.\n"
            f"> Waiting {config.synchronizer.sync_timeout} s for the trainer process...\n"
            "> You can force stop the Trainer process by pressing Ctrl+C.\n"
            "============================================================"
        )
        ray.wait(wait_ref, timeout=config.synchronizer.sync_timeout)
    explorer.shutdown.remote()
    trainer.shutdown.remote()


def activate_data_module(data_processor_url: str, config_path: str):
    """Check whether to activate data module and preprocess datasets."""
    from trinity.cli.client import request

    logger.info(f"Activating data module of {data_processor_url}...")
    res = request(
        url=data_processor_url,
        configPath=config_path,
    )
    if res["return_code"] != 0:
        logger.error(f"Failed to activate data module: {res['return_msg']}.")
        return


def validate_data_pipeline(data_pipeline_config: DataPipelineConfig, pipeline_type: str):
    """
    Check if the data pipeline is valid. The config should:
    1. Non-empty input buffer
    2. Different input/output buffers

    :param data_pipeline_config: the input data pipeline to be validated.
    :param pipeline_type: the type of pipeline, should be one of ["task", "experience"]
    """
    input_buffers = data_pipeline_config.input_buffers
    output_buffer = data_pipeline_config.output_buffer
    # common checks
    # check if the input buffer list is empty
    if len(input_buffers) == 0:
        logger.warning("Empty input buffers in the data pipeline. Won't activate it.")
        return False
    # check if the input and output buffers are different
    input_buffer_names = [buffer.name for buffer in input_buffers]
    if output_buffer.name in input_buffer_names:
        logger.warning("Output buffer exists in input buffers. Won't activate it.")
        return False
    if pipeline_type == "task":
        # task pipeline specific
        # "raw" field should be True for task pipeline because the data source must be raw data files
        for buffer in input_buffers:
            if not buffer.raw:
                logger.warning(
                    'Input buffers should be raw data files for task pipeline ("raw" field should be True). Won\'t activate it.'
                )
                return False
    elif pipeline_type == "experience":
        # experience pipeline specific
        raise NotImplementedError("experience_pipeline is not implemented yet.")
    else:
        logger.warning(
            f'Invalid pipeline type: {pipeline_type}. Should be one of ["task", "experience"].'
        )
        return False
    return True


def run(config_path: str, dlc: bool = False, plugin_dir: str = None):
    load_plugins(plugin_dir)
    config = load_config(config_path)
    config.check_and_update()
    pprint(config)
    # try to activate task pipeline for raw data
    data_processor_config = config.data_processor
    if (
        data_processor_config.data_processor_url
        and data_processor_config.task_pipeline
        and validate_data_pipeline(data_processor_config.task_pipeline, "task")
    ):
        activate_data_module(
            f"{data_processor_config.data_processor_url}/task_pipeline", config_path
        )
    # try to activate experience pipeline for experiences
    if (
        data_processor_config.data_processor_url
        and data_processor_config.experience_pipeline
        and validate_data_pipeline(data_processor_config.experience_pipeline, "experience")
    ):
        activate_data_module(
            f"{data_processor_config.data_processor_url}/experience_pipeline", config_path
        )
    if dlc:
        from trinity.utils.dlc_utils import setup_ray_cluster

        setup_ray_cluster(namespace=config.ray_namespace)
    else:
        from trinity.utils.dlc_utils import is_running

        if not is_running:
            raise RuntimeError("Ray is not running, please start it by `ray start --head`.")
        ray.init(namespace=config.ray_namespace, ignore_reinit_error=True)
    try:
        if config.mode == "explore":
            explore(config)
        elif config.mode == "train":
            train(config)
        elif config.mode == "both":
            both(config)
        elif config.mode == "bench":
            bench(config)
    finally:
        if config.monitor.enable_ray_timeline:
            timeline_file = os.path.join(config.monitor.cache_dir, "timeline.json")
            logger.info(f"Exporting Ray timeline to {timeline_file}...")
            ray.timeline(filename=timeline_file)
            logger.info("Done. You can open the timeline file in `chrome://tracing`")

        if dlc:
            from trinity.utils.dlc_utils import stop_ray_cluster

            stop_ray_cluster(namespace=config.ray_namespace)


def studio(port: int = 8501):
    from streamlit.web import cli as stcli

    current_dir = Path(__file__).resolve().parent.parent
    config_manager_path = os.path.join(current_dir, "manager", "config_manager.py")

    sys.argv = [
        "streamlit",
        "run",
        config_manager_path,
        "--server.port",
        str(port),
        "--server.fileWatcherType",
        "none",
    ]
    sys.exit(stcli.main())


def main() -> None:
    """The main entrypoint."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    run_parser = subparsers.add_parser("run", help="Run RFT process.")
    run_parser.add_argument("--config", type=str, required=True, help="Path to the config file.")
    run_parser.add_argument(
        "--plugin-dir",
        type=str,
        default=None,
        help="Path to the directory containing plugin modules.",
    )
    run_parser.add_argument(
        "--dlc", action="store_true", help="Specify when running in Aliyun PAI DLC."
    )

    # studio command
    studio_parser = subparsers.add_parser("studio", help="Run studio.")
    studio_parser.add_argument(
        "--port", type=int, default=8501, help="The port for Trinity-Studio."
    )

    args = parser.parse_args()
    if args.command == "run":
        # TODO: support parse all args from command line
        run(args.config, args.dlc, args.plugin_dir)
    elif args.command == "studio":
        studio(args.port)


if __name__ == "__main__":
    main()
