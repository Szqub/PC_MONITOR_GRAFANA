"""
ByteTech Agent - entry point.
Handles system signals (SIGTERM, SIGINT) for graceful shutdown.
"""
import logging
import signal
import sys
import os
from typing import Optional

from bytetech_agent.config import load_config
from bytetech_agent.logging_setup import setup_logging
from bytetech_agent.services.scheduler import AgentScheduler

_scheduler: Optional[AgentScheduler] = None


def _signal_handler(signum, _frame):
    """System signal handler for graceful shutdown."""
    sig_name = signal.Signals(signum).name
    logging.getLogger("bytetech_agent").info(f"Received signal {sig_name} - shutting down agent...")
    if _scheduler:
        _scheduler.request_shutdown()


def main():
    global _scheduler

    try:
        # Config path
        config_path = "config.yaml"
        if len(sys.argv) > 1:
            config_path = sys.argv[1]

        # If run from install dir, look for config there
        if not os.path.exists(config_path):
            install_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
            if os.path.exists(install_config):
                config_path = install_config

        config = load_config(config_path)
        setup_logging(config.logging)

        logger = logging.getLogger("bytetech_agent")
        logger.info(f"ByteTech Agent v1.0 - start (config: {os.path.abspath(config_path)})")

        # Register signal handlers
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        # Windows: SIGBREAK
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _signal_handler)

        _scheduler = AgentScheduler(config)
        _scheduler.start()

    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        if _scheduler:
            _scheduler.stop()
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")
        logging.getLogger("bytetech_agent").exception("Critical error in agent application.")
        sys.exit(1)


if __name__ == "__main__":
    main()
